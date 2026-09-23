"""Scheduler and outbound Bot host; exposes no HTTP listener."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .process_lock import exclusive_process_lock
from .schedule_manager import ScheduleManager

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_SECONDS = 10
STATUS_MAX_AGE_SECONDS = 60


def runtime_directory(config: dict) -> Path:
    path = Path((config.get("storage", {}) or {}).get("data_dir", "data"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path / "runtime"


def read_service_status(config: dict) -> dict:
    """Read local process evidence without network requests or credentials."""
    path = runtime_directory(config) / "service_status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("status must be an object")
        thread_states = payload.get("bot_threads_alive", {})
        if not isinstance(thread_states, dict):
            raise ValueError("thread states must be an object")
        updated = datetime.fromisoformat(payload["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        payload["fresh"] = 0 <= age <= STATUS_MAX_AGE_SECONDS
        payload["ready"] = bool(
            payload.get("state") == "running"
            and payload["fresh"]
            and not payload.get("failure")
            and all(thread_states.values())
        )
        return payload
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "unavailable", "ready": False, "fresh": False}


class RuntimeService:
    """Own the lifecycle of one scheduler and all configured Bot receivers."""

    def __init__(self, config: dict, *, scheduling: bool = True):
        self.config = config
        self.scheduling = scheduling
        self.stop_event = threading.Event()
        self.scheduler = ScheduleManager(config) if scheduling else None
        self.bots: dict[str, object] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.failure: str | None = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.status_path = runtime_directory(config) / "service_status.json"

    def _configure_bots(self) -> None:
        settings = self.config.get("interactive", {}) or {}
        if (settings.get("telegram", {}) or {}).get("enabled", False):
            from ..interactive.telegram_bot import TelegramBot

            self.bots["telegram"] = TelegramBot(self.config)
        if (settings.get("feishu", {}) or {}).get("enabled", False):
            from ..interactive.feishu_bot import FeishuBot

            self.bots["feishu"] = FeishuBot(self.config)
        if not self.scheduling and not self.bots:
            raise ValueError("interactive mode requires at least one enabled Bot")
        for bot in self.bots.values():
            bot.validate_config()

    def _run_bot(self, name: str, bot) -> None:
        try:
            bot.run()
            if not self.stop_event.is_set():
                raise RuntimeError(f"{name} receiver stopped unexpectedly")
        except Exception:
            if not self.stop_event.is_set():
                self.failure = f"{name} receiver failed"
                logger.exception("%s", self.failure)
                self.stop_event.set()

    def _write_status(self, state: str) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "state": state,
            "started_at": self.started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "scheduler_enabled": self.scheduling,
            "bot_threads_alive": {
                name: thread.is_alive() for name, thread in self.threads.items()
            },
            "failure": self.failure,
        }
        temporary = self.status_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.status_path)
        finally:
            temporary.unlink(missing_ok=True)

    def stop(self) -> None:
        self.stop_event.set()

    def run(self, on_started: Callable[[], None] | None = None) -> None:
        with exclusive_process_lock(self.status_path.parent / ".service.lock") as acquired:
            if not acquired:
                raise RuntimeError("another scheduler/Bot service is already running")
            previous_handlers = {}
            try:
                self._configure_bots()
                if threading.current_thread() is threading.main_thread():
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, lambda *_: self.stop())
                if self.scheduler is not None:
                    self.scheduler.start()
                for name, bot in self.bots.items():
                    thread = threading.Thread(
                        target=self._run_bot,
                        args=(name, bot),
                        name=f"bot-{name}",
                        daemon=True,
                    )
                    self.threads[name] = thread
                    thread.start()
                if self.failure:
                    raise RuntimeError(self.failure)
                self._write_status("running")
                if on_started is not None:
                    on_started()
                while not self.stop_event.wait(HEARTBEAT_SECONDS):
                    self._write_status("running")
                if self.failure:
                    raise RuntimeError(self.failure)
            except BaseException:
                self.failure = self.failure or "service startup or runtime failed"
                raise
            finally:
                self.stop()
                for name, bot in self.bots.items():
                    try:
                        bot.stop()
                    except Exception:
                        self.failure = self.failure or f"{name} receiver shutdown failed"
                        logger.exception("Unable to stop %s receiver", name)
                if self.scheduler is not None:
                    try:
                        self.scheduler.stop()
                    except Exception:
                        self.failure = self.failure or "scheduler shutdown failed"
                        logger.exception("Unable to stop scheduler")
                for name, thread in self.threads.items():
                    # Telegram requests have a bounded 5s connect + 10s read timeout.
                    thread.join(timeout=20)
                    if thread.is_alive():
                        self.failure = self.failure or f"{name} receiver did not stop"
                try:
                    self._write_status("failed" if self.failure else "stopped")
                except OSError:
                    self.failure = self.failure or "service status could not be saved"
                    logger.exception("Unable to save final service status")
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
            if self.failure:
                raise RuntimeError(self.failure)
