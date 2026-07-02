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


def handle_help() -> str:
    return (
        "<b>可用命令</b>\n\n"
        "<code>/help</code> — 显示此帮助\n"
        "<code>/list</code> — 查看监控列表\n"
        "<code>/add 代码,代码,...</code> — 批量添加（逗号或空格分隔）\n"
        " 例: <code>/add 601728,GOOG,00883</code>\n"
        "<code>/remove 代码,代码,...</code> — 批量移除\n"
        "<code>/backtest 代码 开始 结束</code> — 回测\n"
        " 例: <code>/backtest 601919 2024-01-01 2024-12-31</code>\n"
        "<code>/save</code> — 保存监控列表到 git\n"
        "<code>/brief [afternoon]</code> — 触发简报（默认早盘）\n"
        "<code>/optimize [v1]</code> — 触发策略优化（默认 V2）\n"
        "<code>/daily</code> — 触发完整日报\n"
        "<code>/schedule [任务 时间]</code> — 查看/修改调度时间\n"
        " 例: <code>/schedule daily 20:00</code>\n"
        "<code>/alerts</code> — 查看报警状态\n"
        "<code>/reset_alerts [代码]</code> — 重置报警\n"
        "<code>/mode [frac|position]</code> — 查看/切换策略模式\n"
        "<code>/config [show|set KEY VAL|reset]</code> — 查看/修改优化器配置\n"
        " 例: <code>/config set max_dd -30</code>"
    )


def handle_list() -> str:
    config = _load_config()
    stocks = config.get("stocks", [])
    if not stocks:
        return "监控列表为空。使用 <code>/add 代码</code> 添加。"

    lines = [f"<b>监控列表</b>（共 {len(stocks)} 只）\n"]
    for code in stocks:
        lines.append(f"<code>{code}</code>")
    return "\n".join(lines)


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
