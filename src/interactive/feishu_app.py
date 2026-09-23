"""Outbound Feishu messages and state shared by authenticated SDK events."""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import requests

from ..notification.settings import env_flag
from .command_dispatcher import CommandExecutor
from .security import BotAccess

logger = logging.getLogger(__name__)

FEISHU_API = "https://open.feishu.cn/open-apis"
REQUEST_TIMEOUT = (5, 5)


class FeishuApp:
    """One long-lived application instance; no HTTP callback authentication API."""

    def __init__(self, config: dict):
        self.config = config
        ic = (config.get("interactive") or {}).get("feishu") or {}
        self.app_id = str(ic.get("app_id") or os.getenv("FEISHU_APP_ID", "")).strip()
        self.app_secret = str(
            ic.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")
        ).strip()
        self.access = BotAccess(config, "feishu", allow_wildcard=True)
        self.gate = self.access.gate
        self.rate_limiter = self.access.rate_limiter
        self.allowed_chat_ids = self.access.allowed_chat_ids
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._stopped = threading.Event()
        self.command_executor = CommandExecutor(
            lambda chat_id, text: self.send_message(chat_id, text)
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.access.enabled
            and self.app_id
            and self.app_secret
            and self.allowed_chat_ids
            and not self._stopped.is_set()
        )

    def validate_config(self) -> None:
        self.access.validate_config()
        if not self.app_id or not self.app_secret:
            raise ValueError("Feishu requires app_id and app_secret")

    def _can_send(self) -> bool:
        return (
            self.enabled
            and not env_flag("SKIP_NOTIFICATIONS")
            and not env_flag("SKIP_FEISHU")
        )

    def get_tenant_token(self) -> str:
        if not self._can_send():
            return ""
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at - 60:
                return self._token
            try:
                response = requests.post(
                    f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code") == 0:
                    self._token = data["tenant_access_token"]
                    self._token_expires_at = now + data.get("expire", 7200)
                    return self._token
                logger.error("Feishu token request rejected: code=%s", data.get("code"))
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                logger.error("Feishu token request failed: %s", type(exc).__name__)
            return ""

    def send_message(self, chat_id: str, text: str) -> tuple[bool, str]:
        if not self._can_send():
            return False, "Feishu interactive messages are disabled"
        if not self.gate.is_allowed(chat_id):
            return False, "Chat is not allowed"
        token = self.get_tenant_token()
        if not token:
            return False, "Cannot obtain Feishu tenant token"
        if not self._can_send():
            return False, "Feishu interactive messages are disabled"
        from ..notification.feishu_notifier import _build_interactive_card

        card = _build_interactive_card("股票量化助手", text)
        try:
            response = requests.post(
                f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id",
                json={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return True, "ok"
            return False, f"Feishu code={data.get('code')}"
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.error("Feishu message request failed: %s", type(exc).__name__)
            return False, type(exc).__name__

    def stop(self) -> None:
        self._stopped.set()
        self.command_executor.stop()
