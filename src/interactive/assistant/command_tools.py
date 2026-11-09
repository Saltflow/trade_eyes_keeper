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
    if kind not in {"daily", "brief"} or report_id not in {"morning_snapshot", "afternoon_snapshot"}:
        raise ValueError("需要完整daily/brief报告及有效简报编号")
    return {
        "kind": kind, "report_id": report_id, "force": True,
        "config_sha256": configuration_hash(root),
        "preview": {"report_type": kind, "report_id": report_id,
                    "effect": "按配置完整监控池生成报告并联发全部启用渠道；入队回执不代表送达。"},
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


def prepare_command(root, configuration, text, market=None):
    if not isinstance(text, str):
        raise TypeError("command必须是字符串")
    if len(text) > 512 or any(x in text for x in "\r\n"):
        raise ValueError("每次只接受一条机器人命令")
    command = commands.parse_command(text)
    if isinstance(command, commands.ErrorCommand):
        raise ValueError(command.message)  # noqa: TRY004 - a parsed invalid command
    allowed = (
        commands.HelpCommand,
        commands.ListCommand,
        commands.AddCommand,
        commands.RemoveCommand,
        commands.BacktestCommand,
        commands.SaveCommand,
        commands.DailyReportFrequencyCommand,
        commands.ScheduleCommand,
        commands.ModeCommand,
        commands.RefDateCommand,
        commands.RefPositionCommand,
    )
    if isinstance(command, (commands.DailyCommand, commands.BriefCommand)):
        raise ValueError("报告已迁入report_request；bot_command不再执行报告请求")  # noqa: TRY004
    if not isinstance(command, allowed):
        raise ValueError("该命令未开放对话执行")  # noqa: TRY004
    if isinstance(command, commands.RefDateCommand):
        if Path(root).resolve() != handlers.CONFIG_PATH.parent.parent.resolve():
            raise ValueError("参考持仓配置根目录不一致")
        if not command.date_str:
            return {"kind": "read", "result": {"response": handlers.handle_ref_date()}}
        plan = handlers.prepare_ref_date(command.date_str)
        return {
            "kind": "command",
            "payload": {
                "command": text,
                "config_sha256": configuration_hash(root),
                "reference_reset": plan,
                "preview": plan["preview"],
            },
        }
    if isinstance(command, (commands.AddCommand, commands.RemoveCommand)):
        action = "add" if isinstance(command, commands.AddCommand) else "remove"
        proposals = configuration.propose_watchlist(action, command.codes, market)
        return {"kind": "config", "proposals": proposals}
    if isinstance(command, commands.DailyReportFrequencyCommand) and command.frequency:
        return {
            "kind": "config",
            "proposals": configuration.propose(
                [
                    {
                        "key": "scheduler.daily_report_frequency",
                        "value": command.frequency,
                    }
                ]
            ),
        }
    if isinstance(command, commands.ScheduleCommand) and command.action == "set":
        key = (
            "scheduler.run_time"
            if command.task_id == "daily"
            else (f"scheduler.brief_reports.{command.task_id}.run_time")
        )
        return {
            "kind": "config",
            "proposals": configuration.propose(
                [{"key": key, "value": command.time_str}]
            ),
        }
    if isinstance(command, commands.ListCommand):
        fields = configuration.describe()["fields"]
        return {
            "kind": "read",
            "result": {
                "stocks": next(x["value"] for x in fields if x["key"] == "stocks")
            },
        }
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
        # These queries invoke the same application handlers as slash commands.
        return {"kind": "read", "result": {"response": dispatch_command(command)}}
    if isinstance(command, (commands.BacktestCommand, commands.RefPositionCommand)):
        from ...instruments.resolver import InstrumentResolver

        config = ConfigStore(Path(root) / "config/config.yaml").load_runtime()
        resolver = InstrumentResolver(config)
        if isinstance(command, commands.BacktestCommand):
            code = resolver.resolve(command.stock_code, market)["code"]
            text = f"/backtest {code} {command.start_date} {command.end_date}"
        else:
            code = resolver.resolve(command.code, command.group)["code"]
            text = (
                f"/ref_position {command.action} {command.group} {code} "
                f"{command.shares} {command.price}"
            )
    return {
        "kind": "command",
        "payload": {
            "command": text,
            "config_sha256": configuration_hash(root),
            "preview": {
                "command": text,
                "effect": (
                    "确认后调用现有机器人命令执行器；日报/简报仍运行完整监控池，"
                    "排队回执不代表报告已送达。/save会将配置提交到Git；参考持仓命令会修改参考状态。"
                ),
            },
        },
    }
