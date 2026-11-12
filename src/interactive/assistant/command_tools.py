"""Conversation tools over the existing command/configuration implementations."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ...core.config_store import ConfigStore
from .. import command_parser as commands
from ..command_dispatcher import dispatch_command
from ..commands import handlers


def configuration_hash(root):
    return hashlib.sha256((Path(root) / "config/config.yaml").read_bytes()).hexdigest()


def prepare_report(root, kind="daily", report_id="morning_snapshot"):
    if kind not in {"daily", "brief"} or report_id not in {
        "morning_snapshot",
        "afternoon_snapshot",
    }:
        raise ValueError("需要完整daily/brief报告及有效简报编号")
    return {
        "kind": kind,
        "report_id": report_id,
        "force": True,
        "config_sha256": configuration_hash(root),
        "preview": {
            "report_type": kind,
            "report_id": report_id,
            "effect": "按配置完整监控池生成报告并联发全部启用渠道；入队回执不代表送达。",
        },
    }


def watchlist_preview(payload):
    diff = payload.get("diff", [])
    if len(diff) != 1 or diff[0].get("key") != "stocks":
        return None
    change = diff[0]
    old, new = change["old"], change["new"]
    identities = {x["code"]: x for x in change.get("resolved_instruments", [])}
    lines = []
    for label, codes in (
        ("添加", [x for x in new if x not in old]),
        ("移除", [x for x in old if x not in new]),
    ):
        for code in codes:
            identity = identities.get(code, {})
            name = str(identity.get("name", "")).replace("\n", " ").replace("\r", " ")
            lines.append(
                f"{label}：{name} {code} {identity.get('exchange', identity.get('market', ''))} {identity.get('currency', '')}".strip()
            )
    lines.append(f"监控总数：{len(old)} → {len(new)}；其余标的保留。")
    return "\n".join(lines)


def prepare_reference(root, arguments):
    """Prepare explicit reference business data, never a slash command."""
    import math

    from ...core.ref_portfolio import prepare_reference_reset, reference_status
    from ...instruments.resolver import InstrumentResolver

    path = Path(root) / "config/config.yaml"
    action = arguments.get("action")
    if action == "view":
        if set(arguments) != {"action"}:
            raise ValueError("查看参考持仓只接受 action")
        return {"kind": "read", "result": {"response": reference_status(path)}}
    if action == "reset":
        if set(arguments) != {"action", "date"}:
            raise ValueError("重置参考持仓需要 action、date")
        plan = prepare_reference_reset(path, arguments["date"])
        payload = {
            "action": action,
            "date": arguments["date"],
            "reference_reset": plan,
            "preview": plan["preview"],
        }
    elif action in {"set", "buy", "sell"}:
        if set(arguments) != {"action", "group", "code", "shares", "price"}:
            raise ValueError("修改持仓需要 action、group、code、shares、price")
        if arguments["group"] not in {"a_share", "hk", "us"}:
            raise ValueError("参考持仓市场只能是 a_share、hk、us")
        shares, price = arguments["shares"], arguments["price"]
        if type(shares) is not int or shares < 0 or (action != "set" and shares == 0):
            raise ValueError("股数必须是有效非负整数，买卖必须大于零")
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("成交价必须是正数")
        code = InstrumentResolver(ConfigStore(path).load_runtime()).resolve(
            arguments["code"], arguments["group"]
        )["code"]
        payload = {**arguments, "code": code}
        payload["preview"] = {
            **payload,
            "effect": "按显式成交价修改参考持仓并记录交易；确认后执行",
        }
    else:
        raise ValueError("参考持仓 action 无效")
    payload["config_sha256"] = configuration_hash(root)
    return {"kind": "reference", "payload": payload}


def prepare_configuration_save(root):
    return {
        "kind": "configuration_save",
        "payload": {
            "config_sha256": configuration_hash(root),
            "preview": {"effect": "把当前 config/config.yaml 保存到 Git；确认后执行"},
        },
    }


def prepare_command(root, configuration, text, market=None):
    """Compatibility for queries/backtests only; management has typed tools."""
    if not isinstance(text, str):
        raise TypeError("command必须是字符串")
    if len(text) > 512 or any(x in text for x in "\r\n"):
        raise ValueError("每次只接受一条机器人命令")
    command = commands.parse_command(text)
    if isinstance(command, commands.ErrorCommand):
        raise ValueError(command.message)  # noqa: TRY004 - parsed input error
    if isinstance(command, (commands.DailyCommand, commands.BriefCommand)):
        raise ValueError("报告已迁入report_request；bot_command不再执行报告请求")  # noqa: TRY004
    if isinstance(command, commands.ScheduleCommand) and command.action != "view":
        raise ValueError("固定报告调度入口已停用；请使用 trigger_manage")
    if isinstance(command, commands.DailyReportFrequencyCommand) and command.frequency:
        raise ValueError("固定日报频次入口已停用；请使用 trigger_manage")
    if isinstance(
        command,
        (
            commands.HelpCommand,
            commands.ModeCommand,
            commands.ScheduleCommand,
            commands.DailyReportFrequencyCommand,
        ),
    ):
        if (
            not isinstance(command, commands.HelpCommand)
            and Path(root).resolve() != handlers.CONFIG_PATH.parent.parent.resolve()
        ):
            raise ValueError("查询根目录与共享命令执行器不一致")
        return {"kind": "read", "result": {"response": dispatch_command(command)}}
    if not isinstance(command, commands.BacktestCommand):
        raise ValueError(  # noqa: TRY004 - retired command kind
            "该管理命令已迁入 watchlist_manage、reference_portfolio 或 configuration_save；请重新预览"
        )
    from ...instruments.resolver import InstrumentResolver

    config = ConfigStore(Path(root) / "config/config.yaml").load_runtime()
    code = InstrumentResolver(config).resolve(command.stock_code, market)["code"]
    text = f"/backtest {code} {command.start_date} {command.end_date}"
    return {
        "kind": "command",
        "payload": {
            "command": text,
            "config_sha256": configuration_hash(root),
            "preview": {"command": text, "effect": "确认后执行现有单票回测"},
        },
    }
