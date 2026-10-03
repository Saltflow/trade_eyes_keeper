"""Outbound Feishu messages and state shared by authenticated SDK events."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

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
        self._assistant = None
        self._assistant_lock = threading.RLock()
        self._bot_open_id = str(ic.get("bot_open_id") or "")
        self._bot_info_checked_at = 0.0
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
        if self.assistant_enabled:
            from .assistant.settings import AssistantSettings

            try:
                AssistantSettings.from_config(self.config)
                # Recover owned jobs at startup, even before a new message.
                self.get_assistant()
            except Exception as exc:  # noqa: BLE001 - keep legacy commands available
                logger.error("Feishu assistant startup failed: %s", type(exc).__name__)

    @property
    def assistant_enabled(self) -> bool:
        settings = self.config.get("interactive", {}).get("feishu", {}) or {}
        return (settings.get("assistant") or {}).get("enabled") is True

    def get_assistant(self):
        if not self.assistant_enabled or self._stopped.is_set():
            return None
        with self._assistant_lock:
            if self._assistant is None and not self._stopped.is_set():
                from .assistant.service import FeishuAssistant

                assistant = FeishuAssistant(
                    self.config, self.send_message, self.send_file
                )
                if self._stopped.is_set():
                    assistant.stop()
                    return None
                self._assistant = assistant
                logger.info("Feishu assistant initialized")
            return self._assistant

    def get_bot_open_id(self) -> str:
        """Resolve mentions against this authenticated bot, not any mentioned user."""
        if self._bot_open_id:
            return self._bot_open_id
        if time.monotonic() - self._bot_info_checked_at < 60:
            return ""
        self._bot_info_checked_at = time.monotonic()
        token = self.get_tenant_token()
        if not token:
            return ""
        try:
            response = requests.get(
                f"{FEISHU_API}/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                self._bot_open_id = str((data.get("bot") or {}).get("open_id") or "")
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("Feishu bot identity failed: %s", type(exc).__name__)
        return self._bot_open_id

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
            logger.warning("Feishu message suppressed: delivery_disabled")
            return False, "Feishu interactive messages are disabled"
        if not self.gate.is_allowed(chat_id):
            logger.warning("Feishu message suppressed: chat_not_allowed")
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
                logger.info("Feishu message delivered: kind=interactive")
                return True, "ok"
            # Never log API response bodies, credentials, or conversation contents.
            code = data.get("code")
            logger.error(
                "Feishu message rejected: code=%s",
                code if isinstance(code, int) else "invalid",
            )
            return False, f"Feishu code={data.get('code')}"
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.error("Feishu message request failed: %s", type(exc).__name__)
            return False, type(exc).__name__

    def send_file(self, chat_id: str, path: Path) -> tuple[bool, str]:
        """Upload one verified assistant artifact and send its platform file key."""
        if not self._can_send() or not self.gate.is_allowed(chat_id):
            return False, "Feishu delivery is disabled or unauthorized"
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return False, "Artifact is unavailable"
        if path.stat().st_size > 20 * 1024 * 1024:
            return False, "Artifact exceeds the 20 MiB delivery limit"
        token = self.get_tenant_token()
        if not token:
            return False, "Cannot obtain Feishu tenant token"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with path.open("rb") as handle:
                upload = requests.post(
                    f"{FEISHU_API}/im/v1/files",
                    data={"file_type": "stream", "file_name": path.name},
                    files={"file": (path.name, handle, "application/octet-stream")},
                    headers=headers,
                    timeout=(5, 60),
                )
            upload.raise_for_status()
            data = upload.json()
            if data.get("code") != 0:
                return False, f"Feishu file upload code={data.get('code')}"
            file_key = data["data"]["file_key"]
            if not self._can_send():
                return False, "Feishu delivery is stopped"
            response = requests.post(
                f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id",
                json={
                    "receive_id": chat_id,
                    "msg_type": "file",
                    "content": json.dumps({"file_key": file_key}),
                },
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            result = response.json()
            return (
                (True, "ok")
                if result.get("code") == 0
                else (False, f"Feishu file send code={result.get('code')}")
            )
        except (
            requests.RequestException,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            logger.warning("Feishu file delivery failed: %s", type(exc).__name__)
            return False, type(exc).__name__

    def stop(self) -> None:
        self._stopped.set()
        self.command_executor.stop()
        with self._assistant_lock:
            if self._assistant is not None:
                self._assistant.stop()
