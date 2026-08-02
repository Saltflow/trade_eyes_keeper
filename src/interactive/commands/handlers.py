"""命令处理器 — 每个命令接收解析后的对象，返回响应文本。"""

import logging
import yaml
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "config.yaml"


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        logger.exception(f"读取配置失败: {CONFIG_PATH}")
        return {}


def _save_config(config: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(
                config, f, allow_unicode=True, default_flow_style=False, sort_keys=False
            )
        tmp.replace(CONFIG_PATH)
        logger.info(f"配置已保存: {CONFIG_PATH}")
    except Exception:
        logger.exception(f"保存配置失败: {tmp} -> {CONFIG_PATH}")


def _git_info() -> str:
    """返回当前部署的更新日期 + 最近3条 commit（供 /help 展示）。"""
    import subprocess

    root = CONFIG_PATH.parent.parent
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "-3",
                "--pretty=format:%cd %h %s",
                "--date=format:%m-%d %H:%M",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return ""
        lines = ["\n\n<b>版本信息</b>"]
        for ln in out.stdout.strip().split("\n"):
            # 截断过长的 commit 描述
            lines.append(f"<code>{ln[:70]}</code>")
        return "\n".join(lines)
    except Exception:
        return ""


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
                    "/backtest 代码 起 止",
                    "回测 例 <code>/backtest 601919 2024-01-01 2024-12-31</code>",
                ),
            ],
        ),
        (
            "🔬 策略与搜参",
            [
                ("/optimize", "优化配置中的活动策略并推送三市场候选报告"),
                ("/switch_optimizer [策略]", "查看/切换下次候选策略"),
                ("/mode", "查看当前 TradePlan 执行合同"),
                ("/config [show|set K V|reset]", "查看/修改优化器配置"),
                ("/ref_date [YYYY-MM-DD]", "设置参考持仓基期（默认今天）"),
            ],
        ),
        (
            "🎯 标的开关",
            [
                (
                    "/skip search|signals 代码",
                    "关闭标的搜参/信号 例 <code>/skip search 601985</code>",
                ),
                ("/unskip search|signals 代码", "恢复搜参/信号"),
            ],
        ),
        (
            "🔔 报警与调度",
            [
                ("/alerts", "查看报警状态"),
                ("/reset_alerts [代码]", "重置报警"),
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

    skip_search = {str(c) for c in (config.get("skip_search") or [])}
    skip_signals = {str(c) for c in (config.get("skip_signals") or [])}

    lines = [f"<b>监控列表</b>（共 {len(stocks)} 只）"]
    lines.append("标记: 🔍搜参 📊信号 (划掉=已关闭)\n")
    for code in stocks:
        c = str(code)
        s1 = "🔍" if c not in skip_search else "<s>🔍</s>"
        s2 = "📊" if c not in skip_signals else "<s>📊</s>"
        lines.append(f"<code>{code}</code> {s1}{s2}")
    n_skip_s = len(skip_search)
    n_skip_g = len(skip_signals)
    if n_skip_s or n_skip_g:
        lines.append(f"\n不搜参: {n_skip_s} 只 | 不显示信号: {n_skip_g} 只")
    return "\n".join(lines)


def handle_skip(kind: str, codes: list[str], remove: bool = False) -> str:
    """管理 skip_search / skip_signals 列表。

    Args:
        kind: "search" 或 "signals"
        codes: 标的代码列表
        remove: True=移出skip(恢复), False=加入skip
    """
    key = "skip_search" if kind == "search" else "skip_signals"
    label = "搜参" if kind == "search" else "信号"
    config = _load_config()
    cur = [str(c) for c in (config.get(key) or [])]
    cur_set = {c.upper() for c in cur}
    stocks_upper = {str(s).upper() for s in config.get("stocks", [])}

    changed = []
    for code in codes:
        cu = code.upper()
        if remove:
            match = next((c for c in cur if c.upper() == cu), None)
            if match:
                cur.remove(match)
                cur_set.discard(cu)
                changed.append(code)
        else:
            if cu not in stocks_upper:
                continue  # 不在监控列表，忽略
            if cu not in cur_set:
                cur.append(code)
                cur_set.add(cu)
                changed.append(code)

    if not changed:
        return f"无变更（{label}）。"

    config[key] = cur
    _save_config(config)
    action = "恢复" if remove else "关闭"
    codes_str = " ".join(f"<code>{c}</code>" for c in changed)
    return (
        f"✅ 已{action}{len(changed)} 只标的的{label}: {codes_str}\n"
        f"当前不{label}: {len(cur)} 只"
    )


def _engine_brief(engine_key: str) -> str:
    """获取引擎买卖标准简介。"""
    from ...strategy import get_strategy
    s = get_strategy(engine_key)
    if s:
        return f"{s.label}\n  原理: {s.description}"
    return ""


def handle_switch_optimizer(kind: str | None = None) -> str:
    """切换搜参引擎。kind=None → 列出可用引擎。"""
    from ...strategy import list_strategies, get_strategy
    strategies = list_strategies()

    if kind is None:
        config = _load_config()
        cur = (config.get("optimizer", {}) or {}).get("engine", "percentile")
        lines = [
            "<b>可用候选策略</b>\n",
            "这里只决定下一次 /optimize 搜索哪个策略；不会切换当前生产运行。\n",
        ]
        for s in strategies:
            marker = "  ← 当前" if s["key"] == cur else ""
            lines.append(f"<b>{s['key']}</b> — {s['label']}{marker}")
            lines.append(f"  {s['description']}")
            if s["key"] != cur:
                lines.append(f"  切换: <code>/switch_optimizer {s['key']}</code>")
            lines.append("")
        return "\n".join(lines).rstrip()

    s = get_strategy(kind)
    if s is None:
        valid = [x["key"] for x in strategies]
        return f"❌ 未知引擎: {kind}。可用: {', '.join(valid)}"

    config = _load_config()
    old = (config.get("optimizer", {}) or {}).get("engine", "percentile")
    config.setdefault("optimizer", {})["engine"] = kind
    _save_config(config)
    return (
        f"✅ 下次搜参候选策略: <b>{old} → {kind}</b>\n\n"
        f"{s.label}: {s.description}\n\n"
        "当前生产策略不会改变；候选通过 Gate 后仍须显式激活。"
    )


def handle_add(codes: list[str]) -> str:
    config = _load_config()
    stocks: list[str] = config.get("stocks", [])
    upper_stocks = {str(s).upper() for s in stocks}

    added = []
    skipped = []
    for code in codes:
        if code.upper() in upper_stocks:
            skipped.append(code)
        else:
            stocks.append(code)
            upper_stocks.add(code.upper())
            added.append(code)

    if not added and not skipped:
        return "没有可添加的标的。"

    if added:
        config["stocks"] = stocks
        _save_config(config)

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
    config = _load_config()
    stocks: list[str] = config.get("stocks", [])
    upper_stocks = {str(s).upper(): s for s in stocks}

    removed = []
    not_found = []
    for code in codes:
        matched = upper_stocks.get(code.upper())
        if matched is not None:
            stocks.remove(matched)
            del upper_stocks[code.upper()]
            removed.append(code)
        else:
            not_found.append(code)

    if not removed and not not_found:
        return "没有可移除的标的。"

    if removed:
        config["stocks"] = stocks
        _save_config(config)

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


def _get_backtest_strategy(config: dict):
    """Resolve the newest complete strategy run before consulting defaults."""
    from ...search.artifacts import load_latest_strategy_run
    from ...strategy import get_strategy

    active = load_latest_strategy_run()
    if active and active.strategy is not None:
        return active.strategy

    names = [
        (config.get("optimizer", {}) or {}).get("engine"),
        (config.get("dashboard", {}) or {}).get("strategy"),
        "percentile",
    ]
    for name in names:
        if not name:
            continue
        strategy = get_strategy(str(name))
        if strategy is not None:
            return strategy
    return None


def _get_backtest_params(group: str, strategy):
    """Load group-specific optimized parameters or use neutral valid values."""
    from ...strategy import Params
    from ...search.artifacts import load_latest_strategy_run

    active = load_latest_strategy_run()
    if active and active.strategy_name == strategy.name:
        params = active.params_by_group.get(group)
        if params is not None:
            return params, False

    path = CONFIG_PATH.parent.parent / "data" / "optimizer" / f"{group}_best_params.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Cannot load backtest parameters from %s: %s", path, exc)
        data = {}

    raw_params = data.get("params") if isinstance(data, dict) else None
    if data.get("engine") == strategy.name and isinstance(raw_params, dict):
        try:
            return (
                Params(
                    values={
                        key: int(value)
                        for key, value in raw_params.items()
                        if not key.startswith("_")
                    },
                    _engine=strategy.name,
                ),
                False,
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid backtest parameters in %s: %s", path, exc)

    neutral_values = {
        dim.name: max((dim.levels - 1) // 2, 0)
        for dim in strategy.param_space.dims
    }
    return Params(values=neutral_values, _engine=strategy.name), True


def handle_backtest(code: str, start: str, end: str) -> str:
    """单票回测（使用统一评估引擎 evaluate_all_groups）。"""
    try:
        from ...data.data_source import DataSource
        from ...backtest.engine import evaluate_all_groups
        from ...search.config import get_execution_config
        from ...markets import _detect_fine_group
        import pandas as pd

        config = _load_config()
        ds = DataSource(config)

        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
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

        strategy = _get_backtest_strategy(config)
        if strategy is None:
            return "❌ 未配置有效的回测策略"
        group = _detect_fine_group(code)
        params, using_fallback_params = _get_backtest_params(group, strategy)
        reports = evaluate_all_groups(
            {code: history[history["date"] <= pd.Timestamp(end)]},
            [code],
            strategy,
            params,
            get_execution_config(),
            target_groups=[group],
            start_date=start,
            end_date=end,
        )
        report = reports.get(group)
        if report is None:
            return f"❌ <code>{code}</code> 评估失败，无可交易数据"

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


def _run_main(command_args: list[str], env_extra: dict | None = None) -> str:
    """后台启动 main.py 子进程。返回提示消息。"""
    import subprocess
    import sys
    import os
    from pathlib import Path

    project_root = Path(__file__).parent.parent.parent.parent
    main_py = project_root / "main.py"
    cmd = [sys.executable, str(main_py)] + command_args
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        log_file = project_root / "logs" / "quant_system.log"
        log_fh = open(str(log_file), "a")
        subprocess.Popen(
            cmd,
            cwd=str(project_root),
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
        logger.info(f"后台进程已启动: {' '.join(cmd)}")
        return True
    except Exception:
        logger.exception(f"后台进程启动失败: {cmd}")
        return False


def handle_brief(report_id: str = "morning_snapshot") -> str:
    label = "早盘简报" if report_id == "morning_snapshot" else "收盘简报"
    if _run_main(["--brief", report_id], env_extra={"BOT_FORCE": "1"}):
        return f"⏳ {label}已触发。稍后飞书会推送简报卡片。"
    return f"❌ {label}触发失败"


def handle_optimize() -> str:
    if _run_main(["--optimize"]):
        return "⏳ 策略优化已在后台启动。完成后自动推送三市场简报。"
    return "❌ 策略优化启动失败"


def handle_daily() -> str:
    if _run_main(["--once"], env_extra={"BOT_FORCE": "1"}):
        return "⏳ 完整日报已触发。稍后飞书+邮件会推送。"
    return "❌ 日报触发失败"


def handle_schedule(action: str, task_id: str, time_str: str) -> str:
    """查看或修改调度时间。"""
    # 从 health server 全局实例获取 ScheduleManager
    try:
        from ...health_server.core.global_instances import get_schedule_manager

        mgr = get_schedule_manager()
    except Exception:
        return "❌ 调度管理器未启动"

    if action == "view" or not task_id:
        items = mgr.get_schedule()
        if not items:
            return "当前无调度任务"
        lines = ["<b>当前调度</b>\n"]
        for s in items:
            lines.append(f"<code>{s['name']}</code>: {s['time']}")
        return "\n".join(lines)

    # set
    ok = mgr.reschedule(task_id, time_str)
    if ok:
        label = {
            "daily": "日报",
            "morning_snapshot": "早盘简报",
            "afternoon_snapshot": "收盘简报",
            "optimize": "策略优化",
        }.get(task_id, task_id)
        return f"✅ {label}时间已改为 <code>{time_str}</code>（立即生效）"
    return f"❌ 修改失败。任务名: {task_id}，时间: {time_str}"


def handle_alerts() -> str:
    """查看当前报警状态。"""
    import json
    from pathlib import Path

    state_path = Path("cache/alerts/alerts_state.json")
    if not state_path.exists():
        return "暂无报警状态记录"

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return "报警状态文件读取失败"

    alerts = data.get("alerts", {})
    if not alerts:
        return "当前无活跃报警状态"

    lines = [f"<b>报警状态</b>（共 {len(alerts)} 条）\n"]
    for key, info in alerts.items():
        parts = key.split("_", 2)
        code = parts[0] if parts else "?"
        anchor = parts[1] if len(parts) > 1 else "?"
        interval = parts[2] if len(parts) > 2 else "?"
        days = info.get("consecutive_days", 0)
        suppressed = " ⚠️ 已抑制" if days > 5 else ""
        lines.append(
            f"<code>{code}</code>  {anchor}  {interval}  连续 {days} 天{suppressed}"
        )
    return "\n".join(lines)


def handle_reset_alerts(stock_code: str = "") -> str:
    """清零报警状态。"""
    import json
    from pathlib import Path

    state_path = Path("cache/alerts/alerts_state.json")
    if not state_path.exists():
        return "暂无报警状态记录，无需重置"

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return "报警状态文件读取失败"

    alerts = data.get("alerts", {})
    if not alerts:
        return "当前无报警状态，无需重置"

    if stock_code:
        # 定向清除
        keys_to_delete = [k for k in alerts if k.startswith(f"{stock_code}_")]
        if not keys_to_delete:
            return f"<code>{stock_code}</code> 无报警状态记录"
        for k in keys_to_delete:
            del alerts[k]
        data["alerts"] = alerts
        state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"✅ 已重置 <code>{stock_code}</code> 的报警状态（清除 {len(keys_to_delete)} 条）"
    else:
        # 全部清除
        data["alerts"] = {}
        state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"✅ 已重置所有报警状态（清除 {len(alerts)} 条）"


# ── /mode 和 /config ────────────────────

OPT_CONSTRAINTS_PATH = (
    Path(__file__).parent.parent.parent.parent / "config" / "optimizer_constraints.yaml"
)


def _load_opt_config() -> dict:
    try:
        with open(OPT_CONSTRAINTS_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        logger.exception(f"读取优化器配置失败: {OPT_CONSTRAINTS_PATH}")
        return {}


def _save_opt_config(config: dict) -> None:
    """Validate a temporary optimizer YAML before atomically replacing it."""
    tmp = OPT_CONSTRAINTS_PATH.with_suffix(".yaml.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(
                config, f, allow_unicode=True, default_flow_style=False, sort_keys=False
            )
        from ...search.config import load_constraints, reload_constraints

        load_constraints(tmp)
        tmp.replace(OPT_CONSTRAINTS_PATH)
        reload_constraints()
        logger.info(f"优化器配置已保存: {OPT_CONSTRAINTS_PATH}")
    except Exception as e:
        logger.exception(f"保存优化器配置失败: {e}")
        if tmp.exists():
            tmp.unlink()
        raise


_MODE_LABELS = {
    "trade_plan": "TradePlan（策略声明、统一执行）",
}

_CONFIG_HELP = {
    "solver": {
        "label": "搜参算法",
        "kind": "solver",
    },
    "budget": {
        "label": "当前算法评价预算",
        "kind": "solver_budget",
        "min": 100,
        "max": 1000000,
    },
    "gate_profile": {
        "label": "候选 Gate Profile",
        "kind": "gate_profile",
    },
    "positive_windows": {
        "label": "正收益窗口数",
        "kind": "gate_rule",
        "rule": "positive_return_windows",
        "min": 0,
        "max": 11,
    },
    "majority_windows": {
        "label": "战胜任意两个基准窗口数",
        "kind": "gate_rule",
        "rule": "majority_benchmark_win_windows",
        "min": 0,
        "max": 11,
    },
    "min_pos": {
        "label": "最低平均仓位%",
        "kind": "gate_rule",
        "rule": "minimum_average_position",
        "min": 0.0,
        "max": 100.0,
    },
    "max_dd": {
        "label": "最差窗口最大回撤%",
        "kind": "gate_rule",
        "rule": "maximum_drawdown",
        "min": -100.0,
        "max": 0.0,
    },
    "window_range_penalty": {
        "label": "最好/最差窗口波动惩罚",
        "kind": "path",
        "path": ("walk_forward", "window_range_penalty"),
        "min": 0.0,
        "max": 10.0,
    },
    "workers": {
        "label": "并行评估进程数",
        "kind": "workers",
        "min": 1,
        "max": 128,
    },
    "batch_size": {
        "label": "候选批大小",
        "kind": "path_int",
        "path": ("search", "batch_size"),
        "min": 128,
        "max": 512,
    },
    "buy_cash_levels": {
        "label": "买入现金档位",
        "kind": "levels",
        "path": ("simplified_search", "buy_limit_levels"),
    },
    "sell_cash_levels": {
        "label": "卖出现金档位",
        "kind": "levels",
        "path": ("simplified_search", "sell_limit_levels"),
    },
    "commission": {
        "label": "手续费率",
        "kind": "path",
        "path": ("execution_params", "commission_rate"),
        "min": 0.0,
        "max": 0.02,
    },
    "init_capital": {
        "label": "初始本金",
        "kind": "path",
        "path": ("execution_params", "initial_capital"),
        "min": 10000.0,
        "max": 10000000.0,
    },
}


def _gate_rule(cfg: dict, rule_id: str) -> dict:
    profile_id = str((cfg.get("search", {}) or {}).get("gate_profile", "standard"))
    profile = (cfg.get("gate_profiles", {}) or {}).get(profile_id, {})
    rules = profile.get("rules", []) if isinstance(profile, dict) else []
    for rule in rules:
        if isinstance(rule, dict) and rule.get("id") == rule_id:
            return rule
    raise ValueError(f"Gate Profile {profile_id} 不包含规则 {rule_id}")


def _path_get(cfg: dict, path: tuple[str, str]):
    return (cfg.get(path[0], {}) or {}).get(path[1])


def _path_set(cfg: dict, path: tuple[str, str], value) -> None:
    cfg.setdefault(path[0], {})[path[1]] = value


def handle_mode(mode: str) -> str:
    """Show execution models from the active immutable strategy artifacts."""
    if not mode:
        from ...search.artifacts import load_latest_strategy_run

        active = load_latest_strategy_run()
        lines = [
            f"当前模式: <b>{_MODE_LABELS['trade_plan']}</b>",
            "",
            "执行模式由每个已激活参数产物声明，核心回测和参考持仓统一解释。",
            "支持 <code>cash_cap</code> 与 <code>target_weight</code>，不可全局强制切换。",
        ]
        if active is None:
            lines.append("  当前没有完整已激活运行")
        else:
            lines.append(
                f"  运行: <code>{active.run_id}</code> / "
                f"<code>{active.strategy_name}</code>"
            )
            for group, params in active.params_by_group.items():
                execution = params.execution_snapshot
                lines.append(
                    f"  {group}: {execution.get('model', 'cash_cap')}"
                )
        return "\n".join(lines)

    return "⚠️ 执行模式由 TradePlan 声明，不能通过 /mode 修改。"


def _config_value(cfg: dict, key: str):
    spec = _CONFIG_HELP[key]
    kind = spec["kind"]
    search = cfg.get("search", {}) or {}
    if kind == "solver":
        return search.get("solver_id", "genetic")
    if kind == "solver_budget":
        solver_id = str(search.get("solver_id", "genetic"))
        return ((search.get("solvers", {}) or {}).get(solver_id, {}) or {}).get(
            "budget"
        )
    if kind == "gate_profile":
        return search.get("gate_profile", "standard")
    if kind == "gate_rule":
        return _gate_rule(cfg, str(spec["rule"])).get("value")
    if kind == "workers":
        return search.get("workers")
    return _path_get(cfg, spec["path"])


def _set_config_value(cfg: dict, key: str, raw_value: str) -> None:
    spec = _CONFIG_HELP[key]
    kind = spec["kind"]
    if kind == "solver":
        from ...search.registry import list_solvers

        solver_id = raw_value.strip().lower()
        if solver_id not in list_solvers():
            raise ValueError(f"未知 Solver；可用: {', '.join(list_solvers())}")
        cfg.setdefault("search", {})["solver_id"] = solver_id
        return
    if kind == "gate_profile":
        profile_id = raw_value.strip()
        profiles = cfg.get("gate_profiles", {}) or {}
        if profile_id not in profiles:
            raise ValueError(f"未知 Gate Profile；可用: {', '.join(profiles)}")
        cfg.setdefault("search", {})["gate_profile"] = profile_id
        return
    if kind == "workers":
        if raw_value.strip().lower() == "auto":
            cfg.setdefault("search", {})["workers"] = None
            return
        parsed = int(raw_value)
    elif kind == "levels":
        parsed = sorted(
            {
                float(item.strip())
                for item in raw_value.split(",")
                if item.strip()
            }
        )
        if not parsed or parsed[0] <= 0:
            raise ValueError("现金档位必须是逗号分隔的正数")
        _path_set(cfg, spec["path"], parsed)
        return
    elif kind in {"solver_budget", "path_int"} or key in {
        "positive_windows",
        "majority_windows",
    }:
        parsed = int(raw_value)
    else:
        parsed = float(raw_value)

    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"不得小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"不得大于 {maximum}")
    if kind == "solver_budget":
        search = cfg.setdefault("search", {})
        solver_id = str(search.get("solver_id", "genetic"))
        search.setdefault("solvers", {}).setdefault(solver_id, {})["budget"] = parsed
    elif kind == "gate_rule":
        _gate_rule(cfg, str(spec["rule"]))["value"] = parsed
    elif kind == "workers":
        cfg.setdefault("search", {})["workers"] = parsed
    else:
        _path_set(cfg, spec["path"], parsed)


def handle_config(action: str, key: str, value: str) -> str:
    """View or safely update the effective Solver/Gate/runtime configuration."""
    cfg = _load_opt_config()
    if action == "reset":
        defaults = {
            "solver": "local_genetic",
            "budget": "155000",
            "gate_profile": "standard",
            "positive_windows": "6",
            "majority_windows": "6",
            "min_pos": "5",
            "max_dd": "-40",
            "window_range_penalty": "0.5",
            "workers": "auto",
            "batch_size": "256",
            "buy_cash_levels": "10000,20000,30000,40000,50000",
            "sell_cash_levels": "10000,20000,30000,40000,50000",
            "commission": "0.005",
            "init_capital": "100000",
        }
        try:
            for default_key, default_value in defaults.items():
                _set_config_value(cfg, default_key, default_value)
            _save_opt_config(cfg)
        except Exception as exc:
            return f"❌ 默认配置校验失败，未保存: {exc}"
        return "✅ 已恢复统一优化器默认配置"

    if action == "set" and key and value:
        if key not in _CONFIG_HELP:
            return (
                f"❌ 未知配置项: <code>{key}</code>\n"
                f"可用: {', '.join(_CONFIG_HELP)}"
            )
        try:
            _set_config_value(cfg, key, value)
            _save_opt_config(cfg)
        except (TypeError, ValueError) as exc:
            return f"❌ 配置值无效，未保存: {exc}"
        except Exception as exc:
            return f"❌ 配置校验或保存失败，旧配置已保留: {exc}"
        actual = _config_value(cfg, key)
        warning = ""
        if key == "gate_profile":
            profile = (cfg.get("gate_profiles", {}) or {}).get(str(actual), {})
            if not bool((profile or {}).get("activation_eligible", False)):
                warning = "\n⚠️ 此 Profile 只用于探索，候选没有激活资格。"
        return f"✅ {_CONFIG_HELP[key]['label']}: {actual}{warning}"

    if key:
        if key not in _CONFIG_HELP:
            return f"❌ 未知配置项: {key}"
        try:
            current = _config_value(cfg, key)
        except ValueError as exc:
            return f"❌ 当前配置不完整: {exc}"
        if key == "workers" and current is None:
            current = "auto"
        return f"<b>{_CONFIG_HELP[key]['label']}</b>: {current}"

    search = cfg.get("search", {}) or {}
    wf = cfg.get("walk_forward", {}) or {}
    profile_id = str(search.get("gate_profile", "standard"))
    profile = (cfg.get("gate_profiles", {}) or {}).get(profile_id, {}) or {}
    configured = _load_config()
    candidate_strategy = (
        (configured.get("optimizer", {}) or {}).get("engine", "percentile")
    )
    try:
        from ...search.artifacts import load_latest_strategy_run

        active = load_latest_strategy_run()
    except Exception:
        active = None
    active_label = (
        f"{active.strategy_name} / {active.run_id}"
        if active is not None
        else "无完整已激活运行"
    )
    workers = search.get("workers")
    workers_label = "auto（全部物理核）" if workers is None else str(workers)
    try:
        positive = _config_value(cfg, "positive_windows")
        majority = _config_value(cfg, "majority_windows")
        min_pos = _config_value(cfg, "min_pos")
        max_dd = _config_value(cfg, "max_dd")
    except ValueError:
        positive = majority = min_pos = max_dd = "缺失"
    lines = [
        "<b>统一优化配置</b>",
        f"  下次候选策略: {candidate_strategy}",
        f"  当前生产策略: {active_label}",
        f"  Solver: {search.get('solver_id', 'genetic')}",
        f"  预算: {_config_value(cfg, 'budget')}",
        (
            f"  Gate: {profile_id} "
            f"(可激活={bool(profile.get('activation_eligible', False))})"
        ),
        f"  正收益窗口: {positive}/11",
        f"  战胜任意两个基准: {majority}/11",
        f"  最低平均仓位: {min_pos}%  最差回撤: {max_dd}%",
        f"  窗口波动惩罚: {_config_value(cfg, 'window_range_penalty')}",
        f"  workers: {workers_label}  batch: {search.get('batch_size', 256)}",
        (
            "  固定窗口合同: "
            f"{wf.get('data_years', 5) * 12}月 / "
            f"{wf.get('num_windows', 14)}窗 / 11排名+2隔离+1留出"
        ),
        (
            "  现金档位: 买 "
            f"{_config_value(cfg, 'buy_cash_levels')} / 卖 "
            f"{_config_value(cfg, 'sell_cash_levels')}"
        ),
        f"  手续费: {_config_value(cfg, 'commission')}",
        "",
        "修改: <code>/config set KEY VALUE</code>",
        "workers 使用 <code>auto</code> 可占满物理核",
        "可配置项: " + ", ".join(_CONFIG_HELP),
    ]
    return "\n".join(lines)


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
        _dt.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return f"❌ 日期格式错误: {date_str}。请使用 YYYY-MM-DD。"

    from ...search.artifacts import load_latest_strategy_run
    from ...search.config import get_execution_config
    from ...search.contracts import stable_hash

    active_run = load_latest_strategy_run()
    if (
        active_run is None
        or active_run.run_id in {"", "legacy"}
        or active_run.strategy is None
    ):
        return (
            "❌ 没有可绑定的三市场完整已激活运行；参考持仓未重置。"
            "请先完成并激活一次有效搜参运行。"
        )
    execution_config = get_execution_config()
    initial_capital = float(execution_config.initial_capital)

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
            opt["_ref_date_pending"] = date_str
            config["optimizer"] = opt
            _save_config(config)
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
        if params is None:
            return f"❌ 已激活运行缺少 {label} 参数；参考持仓未重置。"
        mgr = RefPortfolioManager(file_path=fname)
        mgr.reset(
            initial_capital=initial_capital,
            inception_date=date_str,
            market_group=group,
            strategy_run_id=active_run.run_id,
            strategy_id=active_run.strategy_name,
            strategy_timestamp=active_run.timestamp,
            params_hash=stable_hash(
                {
                    "strategy_id": active_run.strategy_name,
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
    opt["reference_base_date"] = date_str
    config["optimizer"] = opt
    _save_config(config)
    return (
        f"✅ 参考持仓已重置（A股/港股/美股 三分仓）\n"
        f"📅 基期: <b>{date_str}</b>\n"
        f"💰 每组初始资金: {initial_capital:,.0f}\n"
        f"📭 持仓: 已清空\n"
        f"🔒 固定运行: <code>{active_run.run_id}</code> "
        f"(<code>{active_run.strategy_name}</code>)\n"
        f"\n新搜参不会静默切换参考持仓；再次手动重置才会绑定新运行。"
    )
