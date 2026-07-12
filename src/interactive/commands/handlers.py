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
    except Exception as e:
        logger.exception(f"读取配置失败: {CONFIG_PATH}")
        return {}


def _save_config(config: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.replace(CONFIG_PATH)
        logger.info(f"配置已保存: {CONFIG_PATH}")
    except Exception as e:
        logger.exception(f"保存配置失败: {tmp} -> {CONFIG_PATH}")


def _git_info() -> str:
    """返回当前部署的更新日期 + 最近3条 commit（供 /help 展示）。"""
    import subprocess
    root = CONFIG_PATH.parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-3",
             "--pretty=format:%cd %h %s", "--date=format:%m-%d %H:%M"],
            capture_output=True, text=True, timeout=5,
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
        ("📋 监控列表", [
            ("/list", "查看监控列表"),
            ("/add 代码,...", "批量添加 例 <code>/add 601728,GOOG,00883</code>"),
            ("/remove 代码,...", "批量移除"),
            ("/save", "保存监控列表到 git"),
        ]),
        ("📊 报告触发", [
            ("/daily", "触发完整日报"),
            ("/brief [afternoon]", "触发简报（默认早盘）"),
            ("/backtest 代码 起 止", "回测 例 <code>/backtest 601919 2024-01-01 2024-12-31</code>"),
        ]),
        ("🔬 策略与搜参", [
            ("/optimize [v1]", "触发策略优化（默认 V2）"),
            ("/switch_optimizer [引擎]", "查看/切换搜参引擎 例 <code>/switch_optimizer percentile</code>"),
            ("/mode [frac|position]", "查看/切换策略模式"),
            ("/config [show|set K V|reset]", "查看/修改优化器配置 例 <code>/config set max_dd -30</code>"),
        ]),
        ("🎯 标的开关", [
            ("/skip search|signals 代码", "关闭标的搜参/信号 例 <code>/skip search 601985</code>"),
            ("/unskip search|signals 代码", "恢复搜参/信号"),
        ]),
        ("🔔 报警与调度", [
            ("/alerts", "查看报警状态"),
            ("/reset_alerts [代码]", "重置报警"),
            ("/schedule [任务 时间]", "查看/修改调度 例 <code>/schedule daily 20:00</code>"),
        ]),
        ("ℹ️ 其他", [
            ("/help", "显示此帮助"),
        ]),
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
    return (f"✅ 已{action}{len(changed)} 只标的的{label}: {codes_str}\n"
            f"当前不{label}: {len(cur)} 只")


def handle_switch_optimizer(kind: str | None = None) -> str:
    """切换搜参引擎。

    kind=None → 列出可用引擎。
    kind="global"|"percentile" → 写 config.yaml optimizer.engine。
    """
    engines = {
        "global": "全局阈值引擎 (H1-H6 全旧, ADX/RSI/...绝对值阈值, 默认)",
        "percentile": "分位评分引擎 (§8 新参数化, 松弛H1-H3, 标的自比较分位+权重, 推荐)",
    }

    if kind is None:
        # 列出可用引擎
        config = _load_config()
        cur = (config.get("optimizer", {}) or {}).get("engine", "global")
        lines = ["<b>可用搜参引擎</b>\n"]
        for eng, desc in engines.items():
            marker = " ← 当前" if eng == cur else ""
            example = ("<code>/switch_optimizer global</code>" if eng != cur
                       else "")
            lines.append(f"  <b>{eng}</b> — {desc}{marker}")
            if example:
                lines.append(f"      切换: {example}")
        lines.append(f"\n使用 <code>/switch_optimizer 引擎名</code> 切换")
        return "\n".join(lines)

    if kind not in engines:
        return f"❌ 未知引擎: {kind}。可用: {', '.join(engines.keys())}"

    config = _load_config()
    old = (config.get("optimizer", {}) or {}).get("engine", "global")
    config.setdefault("optimizer", {})["engine"] = kind
    _save_config(config)

    return (f"✅ 搜参引擎已切换: <b>{old} → {kind}</b>\n"
            f"{engines[kind]}\n"
            f"下次 02:00 cron 自动生效。手动搜参: <code>/optimize</code>")


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
        lines.append(f"✅ 已添加 {len(added)} 只：{' '.join(f'<code>{c}</code>' for c in added)}")
    if skipped:
        lines.append(f"⏭ 已存在 {len(skipped)} 只：{' '.join(f'<code>{c}</code>' for c in skipped)}")
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
        lines.append(f"✅ 已移除 {len(removed)} 只：{' '.join(f'<code>{c}</code>' for c in removed)}")
    if not_found:
        lines.append(f"❌ 未找到 {len(not_found)} 只：{' '.join(f'<code>{c}</code>' for c in not_found)}")
    lines.append(f"当前共 {len(stocks)} 只")
    return "\n".join(lines)


def handle_backtest(code: str, start: str, end: str) -> str:
    try:
        from src.data.data_source import DataSource
        from src.analysis.portfolio_strategy import TimingStrategyEngine

        config = _load_config()
        ds = DataSource(config)

        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        requested_days = (e - s).days
        days = max(requested_days + 365, 1000)  # 至少请求约 2.7 年数据给 MA60

        data = ds.fetch_stock_data(code, days=days)
        if data is None or data.empty:
            return f"❌ 未获取到 <code>{code}</code> 的行情数据"

        # 检查实际可用数据范围
        actual_start = str(data["date"].min())[:10]
        actual_end = str(data["date"].max())[:10]
        data = data[(data["date"] >= start) & (data["date"] <= end)]
        if data.empty:
            return (
                f"❌ <code>{code}</code> 在 {start} ~ {end} 无数据\n"
                f"缓存数据范围: {actual_start} ~ {actual_end}"
            )

        # 数据完整性提示
        data_note = ""
        if actual_start > start:
            data_note = (
                f"⚠ 数据不完整：请求 {start}，最早可用 {actual_start}。"
                f"请 <code>/add {code}</code> 后等待系统缓存更久。"
            )

        engine = TimingStrategyEngine(code, data)
        metrics = engine.run_simulation(initial_cash=100000)

        bh_start = float(data["close"].iloc[0])
        bh_end = float(data["close"].iloc[-1])
        bh_return = (bh_end - bh_start) / bh_start * 100

        # 交易统计
        buy_count = sum(1 for t in metrics.trade_log if t.trade_type == "buy")
        sell_count = sum(1 for t in metrics.trade_log if t.trade_type == "sell")
        total_fee = sum(t.fee for t in metrics.trade_log)

        # 最近 3 笔
        recent = ""
        for t in metrics.trade_log[-3:]:
            emoji = "🟢" if t.trade_type == "buy" else "🔴"
            recent += f"{emoji} {t.date} {t.trade_type} {t.shares}股@{t.price:.2f} {t.reason}\n"

        return (
            f"<b>回测报告</b> — <code>{code}</code>\n"
            f"区间: {start} ~ {end}（{len(data)} 天）\n"
            + (f"{data_note}\n" if data_note else "")
            + f"策略: MA60 均值回归（买 ≤-5%/-10%，卖 ≥+5%/+10%/+15%）\n\n"
            f"<b>策略收益</b>: {metrics.total_return:+.2f}%"
            f"  |  <b>买入持有</b>: {bh_return:+.2f}%\n"
            f"<b>年化收益</b>: {metrics.annual_return:+.2f}%"
            f"  |  <b>最大回撤</b>: {metrics.max_drawdown:.2f}%\n"
            f"<b>夏普比率</b>: {metrics.sharpe_ratio:.2f}"
            f"  |  <b>交易</b>: {metrics.total_trades} 笔"
            f"（买{buy_count}/卖{sell_count}）\n"
            f"<b>期末持仓</b>: ¥{metrics.final_position_value:,.0f}"
            f"  |  <b>手续费</b>: ¥{total_fee:,.2f}"
            + (f"\n\n<b>最近交易:</b>\n{recent}" if recent else "")
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
            cwd=repo, check=True, capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", "bot: watchlist updated via /save",
             "--no-verify"],
            cwd=repo, check=True, capture_output=True,
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
        subprocess.Popen(
            cmd, cwd=str(project_root), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"后台进程已启动: {' '.join(cmd)}")
        return True
    except Exception as e:
        logger.exception(f"后台进程启动失败: {cmd}")
        return False


def handle_brief(report_id: str = "morning_snapshot") -> str:
    label = "早盘简报" if report_id == "morning_snapshot" else "收盘简报"
    if _run_main(["--brief", report_id], env_extra={"BOT_FORCE": "1"}):
        return f"⏳ {label}已触发。稍后飞书会推送简报卡片。"
    return f"❌ {label}触发失败"


def handle_optimize(preset: str = "v2") -> str:
    if preset == "v1":
        label = "策略优化 V1（贝叶斯）"
        args = ["--optimize"]
        env = {}
    elif preset == "fast":
        label = "策略优化 V2 快速（~2 分钟）"
        args = ["--optimize-v2"]
        env = {"OPTIMIZER_SAMPLES": "2000", "OPTIMIZER_GENERATIONS": "1"}
    elif preset == "deep":
        label = "策略优化 V2 深度（~30 分钟）"
        args = ["--optimize-v2"]
        env = {"OPTIMIZER_SAMPLES": "20000", "OPTIMIZER_GENERATIONS": "5"}
    else:
        label = "策略优化 V2"
        args = ["--optimize-v2"]
        env = {}

    if _run_main(args, env):
        return f"⏳ {label}已在后台启动。跑完后自动推送到飞书/邮件。"
    return f"❌ {label}启动失败"


def handle_daily() -> str:
    if _run_main(["--once"], env_extra={"BOT_FORCE": "1"}):
        return "⏳ 完整日报已触发。稍后飞书+邮件会推送。"
    return "❌ 日报触发失败"


def handle_schedule(action: str, task_id: str, time_str: str) -> str:
    """查看或修改调度时间。"""
    # 从 health server 全局实例获取 ScheduleManager
    try:
        from src.health_server.core.global_instances import get_schedule_manager
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
        state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"✅ 已重置 <code>{stock_code}</code> 的报警状态（清除 {len(keys_to_delete)} 条）"
    else:
        # 全部清除
        data["alerts"] = {}
        state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"✅ 已重置所有报警状态（清除 {len(alerts)} 条）"


# ── /mode 和 /config ────────────────────

OPT_CONSTRAINTS_PATH = (
    Path(__file__).parent.parent.parent.parent / "config" / "optimizer_constraints.yaml"
)


def _load_opt_config() -> dict:
    try:
        with open(OPT_CONSTRAINTS_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.exception(f"读取优化器配置失败: {OPT_CONSTRAINTS_PATH}")
        return {}


def _save_opt_config(config: dict) -> None:
    tmp = OPT_CONSTRAINTS_PATH.with_suffix(".yaml.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.replace(OPT_CONSTRAINTS_PATH)
        logger.info(f"优化器配置已保存: {OPT_CONSTRAINTS_PATH}")
    except Exception as e:
        logger.exception(f"保存优化器配置失败: {e}")


_MODE_LABELS = {"frac": "Fixed-Frac (固定比例买入)", "position_target": "Position-Target (仓位目标驱动)"}

_CONFIG_HELP = {
    "min_pos": ("min_avg_position_pct", "最低平均仓位%", "hard_constraints", 5, 50),
    "max_dd": ("max_drawdown_pct", "最大回撤% (负数)", "hard_constraints", -50, -5),
    "max_trades": ("max_trades_per_month", "月最大交易次数", "hard_constraints", 1, 200),
    "daily_adjust": ("max_daily_adjust", "日调仓上限 (Position-Target)", "position_model", 0.1, 1.0),
    "data_years": ("data_years", "回测数据年数", "walk_forward", 1, 10),
    "confirm_days": ("buy_confirmation_days_ref", "买入确认天数", "position_model", 1, 5),
    "frac_levels": ("frac_levels", "买入比例档位 (Fixed-Frac)", "discrete_search", None, None),
    "num_buy": ("num_buy_rules", "买入规则槽位数", "discrete_search", 1, 10),
    "num_sell": ("num_sell_rules", "卖出规则槽位数", "discrete_search", 1, 5),
}


def handle_mode(mode: str) -> str:
    """切换或查看策略模式。"""
    cfg = _load_opt_config()
    if not mode:
        current = cfg.get("discrete_search", {}).get("mode", "frac")
        label = _MODE_LABELS.get(current, current)
        lines = [
            f"当前模式: <b>{label}</b>",
            "",
            "可用模式:",
            "  <code>/mode frac</code> — Fixed-Frac (每信号固定比例买入)",
            "  <code>/mode position</code> — Position-Target (仓位目标动态调整)",
        ]
        # Show current key params
        ds = cfg.get("discrete_search", {})
        pm = ds.get("position_model", {})
        hc = cfg.get("hard_constraints", {})
        wf = cfg.get("walk_forward", {})
        if current == "position_target":
            adj = pm.get("max_daily_adjust", 0.10)
            lines.append(f"  日调仓上限: {adj:.0%}  数据年: {wf.get('data_years', 5)}")
        else:
            fl = ds.get("frac_levels", [])
            lines.append(f"  买入比例档位: {fl}  月交易上限: {hc.get('max_trades_per_month', 100)}")
        return "\n".join(lines)

    cfg.setdefault("discrete_search", {})["mode"] = mode
    _save_opt_config(cfg)
    label = _MODE_LABELS.get(mode, mode)
    return f"✅ 已切换为 <b>{label}</b>\n下次 /optimize 将使用此模式"


def handle_config(action: str, key: str, value: str) -> str:
    """查看或修改优化器配置。"""
    cfg = _load_opt_config()

    if action == "reset":
        # Restore defaults
        ds = cfg.setdefault("discrete_search", {})
        ds["frac_levels"] = [0.30, 0.45, 0.60, 0.75, 0.90, 1.00]
        ds["num_buy_rules"] = 5
        ds["num_sell_rules"] = 3
        hc = cfg.setdefault("hard_constraints", {})
        hc["min_avg_position_pct"] = 5
        hc["max_drawdown_pct"] = -40
        hc["max_trades_per_month"] = 100
        pm = ds.setdefault("position_model", {})
        pm["max_daily_adjust"] = 0.40
        wf = cfg.setdefault("walk_forward", {})
        wf["data_years"] = 5
        _save_opt_config(cfg)
        return "✅ 已恢复默认优化器配置"

    if action == "set" and key and value:
        if key not in _CONFIG_HELP:
            return f"❌ 未知配置项: <code>{key}</code>\n可用: {', '.join(_CONFIG_HELP.keys())}"
        field, _, section, vmin, vmax = _CONFIG_HELP[key]
        try:
            if key in ("frac_levels",):
                val = [float(x.strip()) for x in value.split(",")]
            elif key in ("num_buy", "num_sell", "confirm_days"):
                val = int(value)
            else:
                val = float(value)
        except ValueError:
            return f"❌ 值格式错误: {value}"

        if section == "hard_constraints":
            cfg.setdefault("hard_constraints", {})[field] = val
        elif section == "position_model":
            cfg.setdefault("discrete_search", {}).setdefault("position_model", {})[field] = val
        elif section == "walk_forward":
            cfg.setdefault("walk_forward", {})[field] = val
        elif section == "discrete_search":
            cfg.setdefault("discrete_search", {})[field] = val
        _save_opt_config(cfg)
        return f"✅ {_CONFIG_HELP[key][1]}: {value}"

    # show
    ds = cfg.get("discrete_search", {})
    pm = ds.get("position_model", {})
    hc = cfg.get("hard_constraints", {})
    wf = cfg.get("walk_forward", {})
    mode = ds.get("mode", "position_target")
    label = _MODE_LABELS.get(mode, mode)

    if key:
        # show specific
        if key not in _CONFIG_HELP:
            return f"❌ 未知配置项: {key}"
        field, label_f, section, _, _ = _CONFIG_HELP[key]
        val = None
        if section == "hard_constraints":
            val = hc.get(field)
        elif section == "position_model":
            val = pm.get(field)
        elif section == "walk_forward":
            val = wf.get(field)
        elif section == "discrete_search":
            val = ds.get(field)
        return f"<b>{label_f}</b>: {val}"

    lines = [
        f"<b>优化器配置</b> (模式: {label})",
        f"  数据年: {wf.get('data_years', 5)}",
        f"  最低仓位%: {hc.get('min_avg_position_pct', 5)}  最大回撤%: {hc.get('max_drawdown_pct', -40)}",
        f"  月交易上限: {hc.get('max_trades_per_month', 100)}",
    ]
    if mode == "position_target":
        lines.append(f"  日调仓上限: {pm.get('max_daily_adjust', 0.10):.0%}")
    else:
        lines.append(f"  买入比例档位: {ds.get('frac_levels', [])}")
    lines.extend([
        f"  买入槽位: {ds.get('num_buy_rules', 5)}  卖出槽位: {ds.get('num_sell_rules', 3)}",
        "",
        "修改: <code>/config set KEY VALUE</code>",
        "可配置项: " + ", ".join(_CONFIG_HELP.keys()),
    ])
    return "\n".join(lines)
