"""Authenticated transport and identity helpers; no model or worker loop."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path

import yaml


def validate_profile_binding(settings, database):
    """Check immutable scope facts before creating stores or recovering work."""
    database = Path(database)
    if database.is_symlink():
        raise ValueError("测试数据库不能是链接。")
    if not database.exists():
        return
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "test_application" in tables:
            previous = db.execute("SELECT app_id FROM test_application").fetchall()
            if previous and previous != [(settings["app_id"],)]:
                raise ValueError("测试工作区已绑定其他应用。")
        if "group_binding" in tables:
            binding = db.execute("SELECT app_id,chat_id FROM group_binding").fetchall()
            if binding and binding != [(settings["app_id"], settings.get("chat_id"))]:
                raise ValueError("测试工作区已绑定其他应用或群。")


def production_app_ids(root: Path, environ=None) -> set[str]:
    """Read IDs for comparison only. Do not load .env or return any secrets."""
    environ = os.environ if environ is None else environ
    ids = {str(environ.get("FEISHU_APP_ID", "")).strip()}
    config = root / "config" / "config.yaml"
    if config.exists():
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        ids.add(
            str(raw.get("interactive", {}).get("feishu", {}).get("app_id", "")).strip()
        )
    for path in (root / "config" / ".env", root / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(
                    r"\s*(?:export\s+)?FEISHU_APP_ID\s*=\s*([^#]+?)\s*(?:#.*)?", line
                )
                if match:
                    ids.add(match[1].strip().strip("\"'"))
    return {value for value in ids if value}


def message_identity(body: dict):
    if (body.get("header") or {}).get("event_type") != "im.message.receive_v1":
        return None
    event = body.get("event") or {}
    sender = event.get("sender") or {}
    message = event.get("message") or {}
    if sender.get("sender_type") != "user" or message.get("chat_type") != "p2p":
        return None
    owner = (sender.get("sender_id") or {}).get("open_id")
    chat, message_id = message.get("chat_id"), message.get("message_id")
    if not all(isinstance(value, str) and value for value in (owner, chat, message_id)):
        return None
    return owner, chat, message_id, message


def make_transport(settings, *, route_guard=None):
    """Reuse authenticated transport, without constructing a command executor."""
    from src.interactive.feishu_app import FeishuApp
    from src.interactive.security import BotAccess

    class TestTransport(FeishuApp):
        def __init__(self):
            self.config = {
                "interactive": {
                    "feishu": {
                        "enabled": True,
                        "app_id": settings["app_id"],
                        "app_secret": settings["app_secret"],
                        "allowed_chat_ids": settings["allowed_chat_ids"],
                    }
                }
            }
            self.app_id, self.app_secret = settings["app_id"], settings["app_secret"]
            self.access = BotAccess(self.config, "feishu")
            self.gate, self.allowed_chat_ids = (
                self.access.gate,
                self.access.allowed_chat_ids,
            )
            self._token, self._token_expires_at = "", 0.0
            self._bot_open_id, self._bot_info_checked_at = "", 0.0
            self._token_lock, self._stopped = threading.Lock(), threading.Event()

        def get_assistant(self):
            raise RuntimeError("测试桥不开放完整助手。")

        def _deliver_message(self, chat_id, text):
            if route_guard is not None and not route_guard(chat_id):
                return {"outcome": "failed", "detail": "profile_route_not_verified"}
            return super()._deliver_message(chat_id, text)

        def stop(self):
            self._stopped.set()

    return TestTransport()
