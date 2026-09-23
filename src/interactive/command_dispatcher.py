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
    if isinstance(command, commands.OptimizeCommand):
        return handlers.handle_optimize(command.group)
    if isinstance(command, commands.DailyCommand):
        return handlers.handle_daily()
    if isinstance(command, commands.DailyReportFrequencyCommand):
        return handlers.handle_daily_report_frequency(command.frequency)
    if isinstance(command, commands.ScheduleCommand):
        return handlers.handle_schedule(
            command.action, command.task_id, command.time_str
        )
    if isinstance(command, commands.AlertsCommand):
        return handlers.handle_alerts()
    if isinstance(command, commands.ResetAlertsCommand):
        return handlers.handle_reset_alerts(command.stock_code)
    if isinstance(command, commands.ModeCommand):
        return handlers.handle_mode(command.mode)
    if isinstance(command, commands.ConfigCommand):
        return handlers.handle_config(command.action, command.key, command.value)
    if isinstance(command, commands.SkipCommand):
        return handlers.handle_skip(command.kind, command.codes, remove=command.remove)
    if isinstance(command, commands.SwitchOptimizerCommand):
        return handlers.handle_switch_optimizer(command.kind, command.group)
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
        self._stopped = threading.Event()
        self._backtest_slot = threading.BoundedSemaphore(1)

    def execute(self, chat_id: str, command) -> None:
        if self._stopped.is_set():
            return
        if isinstance(command, commands.BacktestCommand):
            if not self._backtest_slot.acquire(blocking=False):
                self._reply(chat_id, "⏳ 已有回测正在执行，请稍后再试。")
                return
            self._reply(chat_id, f"⏳ 正在回测 <code>{command.stock_code}</code>…")
            worker = threading.Thread(
                target=self._run_backtest,
                args=(chat_id, command),
                name="bot-backtest",
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                self._backtest_slot.release()
                raise
            return
        self._run_command(chat_id, command)

    def _run_command(self, chat_id: str, command) -> None:
        try:
            response = dispatch_command(command)
        except Exception:
            logger.exception("Bot command failed: %s", type(command).__name__)
            response = "❌ 命令执行失败，请查看服务日志。"
        self._reply(chat_id, response)

    def _run_backtest(self, chat_id: str, command) -> None:
        try:
            self._run_command(chat_id, command)
        finally:
            self._backtest_slot.release()

    def _reply(self, chat_id: str, text: str) -> None:
        if not self._stopped.is_set():
            self._send_message(chat_id, text)

    def stop(self) -> None:
        self._stopped.set()
