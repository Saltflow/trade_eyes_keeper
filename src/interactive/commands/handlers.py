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
        "<code>/add 代码</code> — 添加股票到监控列表\n"
        "<code>/remove 代码</code> — 从监控列表移除股票\n"
        "<code>/backtest 代码 开始 结束</code> — 回测\n"
        " 例: <code>/backtest 601919 2024-01-01 2024-12-31</code>"
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


def handle_add(code: str) -> str:
    config = _load_config()
    stocks: list[str] = config.get("stocks", [])
    logger.info(f"handle_add: code={code} stocks_before={len(stocks)}")

    if str(code).upper() in (str(s).upper() for s in stocks):
        return f"<code>{code}</code> 已在监控列表中。"

    stocks.append(code)
    config["stocks"] = stocks
    _save_config(config)
    logger.info(f"已添加 {code} 到监控列表，共 {len(stocks)} 只")
    return f"✅ 已添加 <code>{code}</code> 到监控列表（共 {len(stocks)} 只）"


def handle_remove(code: str) -> str:
    config = _load_config()
    stocks: list[str] = config.get("stocks", [])
    logger.info(f"handle_remove: code={code} stocks_before={len(stocks)}")

    matched = None
    for s in stocks:
        if str(s).upper() == str(code).upper():
            matched = s
            break

    if matched is None:
        return f"<code>{code}</code> 不在监控列表中。"

    stocks.remove(matched)
    config["stocks"] = stocks
    _save_config(config)
    logger.info(f"已从监控列表移除 {matched}，剩余 {len(stocks)} 只")
    return f"✅ 已移除 <code>{matched}</code>（剩余 {len(stocks)} 只）"


def handle_backtest(code: str, start: str, end: str) -> str:
    try:
        from src.data.data_source import DataSource
        from src.analysis.portfolio_strategy import TimingStrategyEngine

        config = _load_config()
        ds = DataSource(config)

        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        days = (e - s).days + 60  # 多取 60 天余量给 MA60

        data = ds.fetch_stock_data(code, days=days)
        if data is None or data.empty:
            return f"❌ 未获取到 <code>{code}</code> 的行情数据"

        data = data[(data["date"] >= start) & (data["date"] <= end)]
        if data.empty:
            return f"❌ <code>{code}</code> 在 {start} ~ {end} 无数据"

        engine = TimingStrategyEngine(code, data)
        rules = config.get("portfolio_strategy", {}).get("rules")
        metrics = engine.run_simulation(
            initial_cash=100000,
            rules=rules,
        )

        bh_start = float(data["close"].iloc[0])
        bh_end = float(data["close"].iloc[-1])
        bh_return = (bh_end - bh_start) / bh_start * 100

        return (
            f"<b>回测报告</b> — <code>{code}</code>\n"
            f"区间: {start} ~ {end}（{len(data)} 天）\n\n"
            f"<b>策略收益</b>: {metrics.total_return:+.2f}%\n"
            f"<b>买入持有</b>: {bh_return:+.2f}%\n"
            f"<b>最大回撤</b>: {metrics.max_drawdown:.2f}%\n"
            f"<b>夏普比率</b>: {metrics.sharpe_ratio:.2f}\n"
            f"<b>总交易</b>: {metrics.total_trades}\n"
        )
    except Exception as exc:
        logger.exception(f"回测失败 {code} {start} {end}")
        return f"❌ 回测失败: {exc}"
