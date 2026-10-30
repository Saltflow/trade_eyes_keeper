"""Outbound Feishu messages and state shared by authenticated SDK events."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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

    def __init__(self, config: dict, *, execution_runtime=None):
        self.config = config
        self.execution_runtime = execution_runtime
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
                    self.config,
                    self.send_message,
                    self.send_file,
                    image_loader=self.download_message_image,
                    personal_route_allowed=self.gate.is_allowed,
                    personal_send=self.send_personal_message,
                    execution_runtime=self.execution_runtime,
                )
                if self._stopped.is_set():
                    assistant.stop()
                    return None
                self._assistant = assistant
                logger.info("Feishu assistant initialized")
            return self._assistant

    def download_message_image(self, message_id: str, image_key: str) -> dict:
        """Download one authenticated message image with a bounded response body."""
        from .assistant.position_images import detect_image_mime

        if not self.enabled:
            raise ValueError("飞书截图读取已停用。")
        for value in (message_id, image_key):
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,256}", value
            ):
                raise ValueError("无效的飞书图片资源标识。")
        token = self.get_tenant_token()
        if not token:
            raise ValueError("无法取得飞书图片读取凭据。")
        limit = 10 * 1024 * 1024
        try:
            with requests.get(
                f"{FEISHU_API}/im/v1/messages/{message_id}/resources/{image_key}",
                params={"type": "image"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, 30),
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ValueError("飞书截图资源读取失败。")
                declared = response.headers.get("Content-Length")
                if declared and (not declared.isdigit() or int(declared) > limit):
                    raise ValueError("截图超过 10 MiB 上限。")
                mime = response.headers.get("Content-Type", "").split(";")[0].lower()
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("仅支持 JPEG、PNG、WebP 截图。")
                chunks = []
                size = 0
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("截图超过 10 MiB 上限。")
                    chunks.append(chunk)
                payload = b"".join(chunks)
            if detect_image_mime(payload) != mime:
                raise ValueError("截图文件内容与声明格式不一致。")
            return {
                "image": payload,
                "mime_type": mime,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        except requests.RequestException:
            raise ValueError("飞书截图资源网络读取失败。") from None

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
        """Keep legacy tuple callers on the shared classified delivery path."""
        result = self._deliver_message(chat_id, text)
        return result["outcome"] == "sent", result["detail"]

    def send_personal_message(self, route: str | dict, text: str) -> dict:
        """Do not retry an ambiguous send; its caller persists the outcome."""
        chat_id = route.get("chat_id", "") if isinstance(route, dict) else route
        if not isinstance(chat_id, str) or not chat_id:
            return {"outcome": "failed", "detail": "Invalid delivery route"}
        return self._deliver_message(chat_id, text)

    def _deliver_message(self, chat_id: str, text: str) -> dict:
        if not self._can_send():
            logger.warning("Feishu message suppressed: delivery_disabled")
            return {
                "outcome": "failed",
                "detail": "Feishu interactive messages are disabled",
            }
        if not self.gate.is_allowed(chat_id):
            logger.warning("Feishu message suppressed: chat_not_allowed")
            return {"outcome": "failed", "detail": "Chat is not allowed"}
        token = self.get_tenant_token()
        if not token:
            return {"outcome": "failed", "detail": "Cannot obtain Feishu tenant token"}
        if not self._can_send():
            return {
                "outcome": "failed",
                "detail": "Feishu interactive messages are disabled",
            }
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
            if isinstance(response.status_code, int) and response.status_code != 200:
                return {"outcome": "unknown", "detail": "Unexpected Feishu HTTP status"}
            data = response.json()
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                return {
                    "outcome": "unknown",
                    "detail": "Invalid Feishu delivery response",
                }
            if data["code"] == 0:
                logger.info("Feishu message delivered: kind=interactive")
                message_data = data.get("data")
                message_id = (
                    message_data.get("message_id")
                    if isinstance(message_data, dict)
                    else None
                )
                return {
                    "outcome": "sent",
                    "detail": "ok",
                    "platform_message_id": message_id
                    if isinstance(message_id, str)
                    else None,
                }
            # Never log API response bodies, credentials, or conversation contents.
            code = data.get("code")
            logger.error(
                "Feishu message rejected: code=%s",
                code if isinstance(code, int) else "invalid",
            )
            return {"outcome": "failed", "detail": f"Feishu code={code}"}
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.error("Feishu message request failed: %s", type(exc).__name__)
            return {"outcome": "unknown", "detail": type(exc).__name__}

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
