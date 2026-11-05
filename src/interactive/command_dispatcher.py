"""Shared command execution for all authenticated bot transports."""

import logging
import threading
from collections.abc import Callable

from . import command_parser as commands
from .commands import handlers

logger = logging.getLogger(__name__)


def dispatch_command(command) -> str:
    """Execute an already parsed command through the common application handlers."""
    if isinstance(command, commands.HelpCommand):
        return handlers.handle_help()
    if isinstance(command, commands.ListCommand):
        return handlers.handle_list()
    if isinstance(command, commands.AddCommand):
        return handlers.handle_add(command.codes)
    if isinstance(command, commands.RemoveCommand):
        return handlers.handle_remove(command.codes)
    if isinstance(command, commands.BacktestCommand):
        return handlers.handle_backtest(
            command.stock_code, command.start_date, command.end_date
        )
    if isinstance(command, commands.SaveCommand):
        return handlers.handle_save()
    if isinstance(command, commands.BriefCommand):
        return handlers.handle_brief(command.report_id)
    if isinstance(command, commands.DailyCommand):
        return handlers.handle_daily()
    if isinstance(command, commands.DailyReportFrequencyCommand):
        return handlers.handle_daily_report_frequency(command.frequency)
    if isinstance(command, commands.ScheduleCommand):
        return handlers.handle_schedule(
            command.action, command.task_id, command.time_str
        )
    if isinstance(command, commands.ModeCommand):
        return handlers.handle_mode(command.mode)
    if isinstance(command, commands.RefDateCommand):
        return handlers.handle_ref_date(command.date_str)
    if isinstance(command, commands.RefPositionCommand):
        return handlers.handle_ref_position(
            command.action, command.group, command.code, command.shares, command.price
        )
    if isinstance(command, commands.ErrorCommand):
        return f"❌ {command.message}"
    return "❌ 未知命令。发送 /help 查看可用命令。"


class CommandExecutor:
    """Keep long backtests off the receiver thread, with one active job per bot."""

    def __init__(self, send_message: Callable):
        self._send_message = send_message
        self.project_root = handlers.CONFIG_PATH.parent.parent.resolve()
        self._stopped = threading.Event()
        self._backtest_slot = threading.BoundedSemaphore(1)

    def execute(self, chat_id: str, command, *, on_result=None) -> None:
        if self._stopped.is_set():
            if on_result:
                on_result("❌ 命令执行器正在停止，未开始执行。")
            return
        if isinstance(command, commands.BacktestCommand):
            if not self._backtest_slot.acquire(blocking=False):
                self._reply(chat_id, "⏳ 已有回测正在执行，请稍后再试。")
                if on_result:
                    on_result("❌ 已有回测正在执行，未提交新任务。")
                return
            self._reply(chat_id, f"⏳ 正在回测 <code>{command.stock_code}</code>…")
            worker = threading.Thread(
                target=self._run_backtest,
                args=(chat_id, command, on_result),
                name="bot-backtest",
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                self._backtest_slot.release()
                raise
            return
        self._run_command(chat_id, command, on_result)

    def _run_command(self, chat_id: str, command, on_result=None) -> None:
        try:
            response = dispatch_command(command)
        except Exception:
            logger.exception("Bot command failed: %s", type(command).__name__)
            response = "❌ 命令执行失败，请查看服务日志。"
        self._reply(chat_id, response)
        if on_result:
            on_result(response)

    def _run_backtest(self, chat_id: str, command, on_result=None) -> None:
        try:
            self._run_command(chat_id, command, on_result)
        finally:
            self._backtest_slot.release()

    def _reply(self, chat_id: str, text: str) -> None:
        if not self._stopped.is_set():
            self._send_message(chat_id, text)

    def stop(self) -> None:
        self._stopped.set()
