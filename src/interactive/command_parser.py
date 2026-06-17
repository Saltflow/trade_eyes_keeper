"""Telegram 命令解析器 — 将用户消息解析为命令对象。"""

import re
from dataclasses import dataclass
from enum import Enum, auto


class CommandType(Enum):
    HELP = auto()
    LIST = auto()
    ADD = auto()
    REMOVE = auto()
    BACKTEST = auto()
    SAVE = auto()
    BRIEF = auto()
    OPTIMIZE = auto()
    DAILY = auto()
    ERROR = auto()


@dataclass
class HelpCommand:
    cmd_type: CommandType = CommandType.HELP


@dataclass
class ListCommand:
    cmd_type: CommandType = CommandType.LIST


@dataclass
class AddCommand:
    codes: list[str]
    cmd_type: CommandType = CommandType.ADD

    @property
    def stock_code(self) -> str:
        """向后兼容：取第一只。"""
        return self.codes[0] if self.codes else ""


@dataclass
class RemoveCommand:
    codes: list[str]
    cmd_type: CommandType = CommandType.REMOVE

    @property
    def stock_code(self) -> str:
        return self.codes[0] if self.codes else ""


@dataclass
class BacktestCommand:
    stock_code: str
    start_date: str
    end_date: str
    cmd_type: CommandType = CommandType.BACKTEST


@dataclass
class SaveCommand:
    cmd_type: CommandType = CommandType.SAVE


@dataclass
class BriefCommand:
    report_id: str = "morning_snapshot"
    cmd_type: CommandType = CommandType.BRIEF


@dataclass
class OptimizeCommand:
    preset: str = "v2"
    cmd_type: CommandType = CommandType.OPTIMIZE


@dataclass
class DailyCommand:
    cmd_type: CommandType = CommandType.DAILY


@dataclass
class ErrorCommand:
    message: str
    cmd_type: CommandType = CommandType.ERROR


_STOCK_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,8}(\.[A-Za-z]{1,4})?$")


def _validate_stock_code(code: str) -> str | None:
    """单个股票代码验证（backtest 用）。"""
    if not code:
        return "缺少股票代码"
    if not _STOCK_CODE_RE.match(code):
        return f"股票代码格式无效: {code}，应为 1-8 位字母数字"
    return None


def _split_codes(raw: str) -> list[str]:
    """将逗号/空格分隔的代码拆为列表，统一大写去重。"""
    parts = re.split(r"[,\s]+", raw.strip().upper())
    return list(dict.fromkeys(p for p in parts if p))  # 去重保序


def _validate_codes(raw: str) -> tuple[list[str], str | None]:
    """批量验证股票代码。返回 (codes, error_msg)。"""
    codes = _split_codes(raw)
    if not codes:
        return [], "缺少股票代码，格式: /add 601728,GOOG,00883"
    for code in codes:
        if not _STOCK_CODE_RE.match(code):
            return [], f"股票代码格式无效: {code}，应为 1-8 位字母数字"
    return codes, None
    """验证股票代码格式。返回错误消息或 None（有效）。"""
    if not code:
        return "缺少股票代码，格式: /add 601728"
    if not _STOCK_CODE_RE.match(code):
        return f"股票代码格式无效: {code}，应为 1-8 位字母数字"
    return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(date_str: str) -> str | None:
    """验证日期格式 YYYY-MM-DD，返回错误消息或 None。"""
    if not _DATE_RE.match(date_str):
        return f"日期格式无效: {date_str}，应为 YYYY-MM-DD"
    try:
        from datetime import datetime

        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return f"日期不存在: {date_str}"
    return None


def parse_command(text: str):
    """将用户消息解析为命令对象。

    Args:
        text: 用户消息原文，如 "/add 601728"

    Returns:
        HelpCommand | ListCommand | AddCommand | RemoveCommand |
        BacktestCommand | ErrorCommand
    """
    if not text or not text.strip():
        return ErrorCommand(message="请输入命令。发送 /help 查看可用命令")

    text = text.strip()
    if not text.startswith("/"):
        return ErrorCommand(message="不是命令。发送 /help 查看可用命令")

    parts = text.split(maxsplit=1)
    cmd_name = parts[0][1:].lower()  # 去掉 / 并转小写
    args = parts[1] if len(parts) > 1 else ""

    if cmd_name == "help":
        return HelpCommand()

    if cmd_name == "list":
        return ListCommand()

    if cmd_name == "add":
        codes, err = _validate_codes(args)
        if err:
            return ErrorCommand(message=err)
        return AddCommand(codes=codes)

    if cmd_name == "remove":
        codes, err = _validate_codes(args)
        if err:
            return ErrorCommand(message=err)
        return RemoveCommand(codes=codes)

    if cmd_name == "save":
        return SaveCommand()

    if cmd_name == "brief":
        mode = args.strip().lower()
        if mode in ("afternoon", "afternoon_snapshot"):
            return BriefCommand(report_id="afternoon_snapshot")
        return BriefCommand(report_id="morning_snapshot")

    if cmd_name == "optimize":
        mode = args.strip().lower()
        if mode in ("v1",):
            return OptimizeCommand(preset="v1")
        if mode in ("fast",):
            return OptimizeCommand(preset="fast")
        if mode in ("deep",):
            return OptimizeCommand(preset="deep")
        return OptimizeCommand(preset="v2")

    if cmd_name == "daily":
        return DailyCommand()

    if cmd_name == "backtest":
        arg_parts = args.split()
        if len(arg_parts) < 3:
            return ErrorCommand(
                message="缺少参数。格式: /backtest 601919 2024-01-01 2024-12-31"
            )
        code = arg_parts[0].upper()
        start = arg_parts[1]
        end = arg_parts[2]
        for name, val in [("股票代码", code), ("开始日期", start), ("结束日期", end)]:
            if name == "股票代码":
                err = _validate_stock_code(val)
            else:
                err = _validate_date(val)
            if err:
                return ErrorCommand(message=err)
        return BacktestCommand(stock_code=code, start_date=start, end_date=end)

    return ErrorCommand(
        message=f"未知命令: /{cmd_name}。发送 /help 查看可用命令"
    )
