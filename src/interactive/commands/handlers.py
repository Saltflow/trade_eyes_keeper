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
        "<code>/daily</code> — 触发完整日报"
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


def _run_main(command_args: list[str]) -> str:
    """后台启动 main.py 子进程。返回提示消息。"""
    import subprocess
    from pathlib import Path

    project_root = Path(__file__).parent.parent.parent.parent
    main_py = project_root / "main.py"
    cmd = ["python3", str(main_py)] + command_args
    try:
        subprocess.Popen(
            cmd, cwd=str(project_root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"后台进程已启动: {' '.join(cmd)}")
        return True
    except Exception as e:
        logger.exception(f"后台进程启动失败: {cmd}")
        return False


def handle_brief(report_id: str = "morning_snapshot") -> str:
    label = "早盘简报" if report_id == "morning_snapshot" else "收盘简报"
    if _run_main(["--brief", report_id]):
        return (
            f"⏳ {label}已触发。稍后飞书会推送简报卡片。"
        )
    return f"❌ {label}触发失败"


def handle_optimize(version: str = "v2") -> str:
    label = "策略优化 V2" if version == "v2" else "策略优化 V1（贝叶斯）"
    flag = "--optimize-v2" if version == "v2" else "--optimize"
    if _run_main([flag]):
        return (
            f"⏳ {label}已在后台启动。"
            f"跑完后结果写入 <code>data/optimizer/</code>。"
        )
    return f"❌ {label}启动失败"


def handle_daily() -> str:
    if _run_main(["--once"]):
        return "⏳ 完整日报已触发。稍后飞书+邮件会推送。"
    return "❌ 日报触发失败"
