"""Telegram polling transport for the shared bot command dispatcher."""

from __future__ import annotations

import logging
import math
import os
import threading

import requests

from ..notification.settings import env_flag
from .command_dispatcher import CommandExecutor
from .command_parser import parse_command
from .security import BotAccess

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramBot:
    """A single polling lifecycle; run in a service thread and stop from its owner."""

    def __init__(self, config: dict):
        self.config = config
        ic = (config.get("interactive") or {}).get("telegram") or {}
        self.bot_token = str(
            ic.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        ).strip()
        self.access = BotAccess(config, "telegram")
        self.allowed_chat_ids = self.access.allowed_chat_ids
        self.gate = self.access.gate
        self.rate_limiter = self.access.rate_limiter
        self.polling_interval = float(ic.get("polling_interval", 2))
        if not math.isfinite(self.polling_interval) or self.polling_interval <= 0:
            raise ValueError("interactive.telegram.polling_interval must be positive")
        self._running = False
        self._stop_event = threading.Event()
        self.command_executor = CommandExecutor(
            lambda chat_id, text: self._send_message(chat_id, text)
        )
        proxy_url = (
            ic.get("proxy")
            or os.getenv("TELEGRAM_PROXY")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or ""
        )
        self._proxies = {"https": proxy_url} if proxy_url else None

    @property
    def enabled(self) -> bool:
        return bool(
            self.access.enabled
            and self.bot_token
            and self.allowed_chat_ids
            and not self._stop_event.is_set()
        )

    def validate_config(self) -> None:
        self.access.validate_config()
        if not self.bot_token:
            raise ValueError("Telegram requires bot_token")

    def _api(self, method: str, **params):
        if not self.enabled:
            return None
        if method != "getUpdates" and (
            env_flag("SKIP_NOTIFICATIONS") or env_flag("SKIP_TELEGRAM")
        ):
            return None
        url = f"{TELEGRAM_API}/bot{self.bot_token}/{method}"
        try:
            response = requests.post(
                url,
                data=params,
                timeout=(5, 10),
                proxies=self._proxies,
            )
            if response.status_code != 200:
                logger.error("Telegram API %s HTTP %s", method, response.status_code)
                return None
            result = response.json()
            if not result.get("ok"):
                logger.error("Telegram API %s rejected the request", method)
                return None
            return result.get("result")
        except (requests.RequestException, ValueError, TypeError) as exc:
            # Requests errors may include the token-bearing URL.
            logger.error("Telegram API %s failed: %s", method, type(exc).__name__)
            return None

    def _send_message(self, chat_id: str, text: str) -> bool:
        if not self.gate.is_allowed(chat_id):
            return False
        return (
            self._api(
                "sendMessage",
                chat_id=str(chat_id),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview="true",
            )
            is not None
        )

    def _get_updates(self, offset: int) -> list[dict]:
        result = self._api("getUpdates", offset=offset, timeout=5)
        return result if isinstance(result, list) else []

    def _process_update(self, update: dict) -> None:
        if not self.enabled:
            return
        message = update.get("message") or {}
        text = message.get("text")
        if not isinstance(text, str):
            return
        chat_id = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")
        event_id = f"{chat_id}:{message_id}" if message_id is not None else ""
        result = self.access.accept(chat_id, event_id)
        if result != "accepted":
            if result == "rate_limited":
                self._send_message(chat_id, "操作过于频繁，请稍后再试。")
            return
        command = parse_command(text)
        self.command_executor.execute(chat_id, command)

    def run(self) -> None:
        """Block while polling. A stop waits at most the current bounded HTTP call."""
        self.validate_config()
        if self._stop_event.is_set():
            return
        logger.info("Telegram interactive bot started")
        self._running = True
        offset = 0
        try:
            while not self._stop_event.is_set():
                updates = self._get_updates(offset)
                for update in updates:
                    if self._stop_event.is_set():
                        break
                    try:
                        self._process_update(update)
                    except Exception:
                        logger.exception("Telegram update handling failed")
                    finally:
                        offset = max(offset, update.get("update_id", 0) + 1)
                self._stop_event.wait(self.polling_interval)
        finally:
            self._running = False
            self.command_executor.stop()

    def stop(self) -> None:
        self._stop_event.set()
        self.command_executor.stop()
        self._running = False
