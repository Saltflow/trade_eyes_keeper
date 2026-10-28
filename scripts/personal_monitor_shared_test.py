"""The isolated Feishu profile on the production conversation implementation.

Only transport and scope binding differ. No model loop, reminder loop, HTTP
listener or second WebSocket lives in this adapter. Legacy data is kept in place.
"""

from __future__ import annotations

import sqlite3
import threading

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.personal_monitor import MonitorStore


class AdmissionPaused(RuntimeError):
    """A transport read may finish, but its message has not been admitted."""


class SharedPersonalTestBridge:
    durable_admission = True

    def __init__(
        self,
        settings,
        *,
        transport=None,
        client=None,
        identity_resolver=None,
        execution_runtime,
    ):
        from scripts.personal_monitor_transport import (
            make_transport,
            message_identity,
            validate_profile_binding,
        )

        if execution_runtime is None:
            raise ValueError("测试适配器必须由公共宿主注入运行层。")
        self.settings = settings
        self._admission_paused = threading.Event()
        self._admission_lock = threading.RLock()
        self.transport = transport or make_transport(settings)
        self.identity_resolver = identity_resolver or message_identity
        workspace = settings["workspace"]
        workspace.mkdir(parents=True, exist_ok=True)
        self.database = workspace / "test.sqlite3"
        if self.database.is_symlink():
            raise ValueError("测试数据库不能是链接。")
        if execution_runtime.store.path.resolve() != self.database.resolve():
            raise ValueError("公共宿主必须使用原测试数据库。")
        # Check the binding before creating stores or recovering any delivery.
        validate_profile_binding(settings, self.database)
        self.runtime = execution_runtime
        store = ProposalStore(self.database)
        self.store = MonitorStore(self.database)
        config = {
            "storage": {"data_dir": str(workspace)},
            "llm": {"api_key": settings["api_key"]},
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": settings["app_id"],
                    "allowed_chat_ids": settings["allowed_chat_ids"],
                    "assistant": {"enabled": True, "request_retries": 0},
                }
            },
            "scheduler": {
                "timezone": "Asia/Shanghai",
                "run_time": settings["market_check_times"][-1],
                "brief_reports": [
                    {"run_time": value, "skip_weekends": True}
                    for value in settings["market_check_times"][:-1]
                ],
            },
        }
        # Scope the old dedup facts to this bound profile. Never restore an old
        # model loop or reinterpret an interrupted historical message as intent.
        with sqlite3.connect(self.database) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS test_application (app_id TEXT PRIMARY KEY)"
            )
            previous = db.execute("SELECT app_id FROM test_application").fetchall()
            if previous and previous != [(settings["app_id"],)]:
                raise ValueError("测试工作区已绑定其他应用。")
            db.execute(
                "INSERT OR IGNORE INTO test_application VALUES(?)",
                (settings["app_id"],),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='personal_message_receipts'"
            ).fetchone():
                db.execute(
                    "INSERT OR IGNORE INTO receipts SELECT message_id,0 FROM personal_message_receipts"
                )
        self.assistant = FeishuAssistant(
            config,
            self.transport.send_message,
            store=store,
            personal_store=self.store,
            client=client,
            execution_runtime=self.runtime,
            capabilities={"personal"},
            image_loader=self.transport.download_message_image,
            personal_route_allowed=self.transport.gate.is_allowed,
            personal_send=self.transport.send_personal_message,
        )
        self.health_reporter = None

    def process(self, body):
        with self._admission_lock:
            if self._admission_paused.is_set():
                raise AdmissionPaused("personal profile admission is paused")
            return self._process(body)

    def _process(self, body):
        from src.interactive.feishu_handler import _message_images, _message_text

        identity = self.identity_resolver(body)
        if not identity:
            return "ignored"
        owner, chat, message_id, message = identity
        if (
            owner not in self.settings["allowed_sender_ids"]
            or chat not in self.settings["allowed_chat_ids"]
        ):
            return "unauthorized"
        text, attachments = _message_text(message), _message_images(message)
        mentions = message.get("mentions")
        if mentions is None:
            mentions = []
        if not isinstance(mentions, list):
            return "ignored_foreign_mentions"
        if mentions:
            try:
                bot_id = self.transport.get_bot_open_id()
            except Exception:  # noqa: BLE001 -- identity failures never admit a turn
                return "ignored_foreign_mentions"
            if not isinstance(bot_id, str) or not bot_id.strip():
                return "ignored_foreign_mentions"
            own_keys = []
            for mention in mentions:
                if not isinstance(mention, dict):
                    continue
                identity = mention.get("id")
                key = mention.get("key")
                if (
                    isinstance(identity, dict)
                    and identity.get("open_id") == bot_id
                    and isinstance(key, str)
                    and key
                    and key in text
                ):
                    own_keys.append(key)
            if not own_keys:
                return "ignored_foreign_mentions"
            for key in own_keys:
                text = text.replace(key, "").strip()
        if text.startswith("/"):
            return "legacy_command_disabled"
        if not text and not attachments:
            return "ignored"
        accepted = self.assistant.submit(
            chat,
            owner,
            text,
            message_id,
            attachments=attachments,
            message_time={
                "create_time": message.get("create_time"),
                "update_time": message.get("update_time"),
            },
        )
        return "queued" if accepted else "duplicate_or_rejected"

    def pause_admission(self):
        self._admission_paused.set()
        with self._admission_lock:
            pass

    def stop(self):
        self.pause_admission()
        self.assistant.stop()
        self.transport.stop()
