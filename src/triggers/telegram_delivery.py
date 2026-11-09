"""Configured Telegram reports with the same durable trigger delivery guards."""

from __future__ import annotations

import html
import os
from copy import deepcopy
from pathlib import Path

import yaml

from ..core.config_store import ConfigStore
from ..core.report_execution import runtime_path
from ..interactive.security import BotAccess
from ..notification.settings import channel_enabled
from ..notification.telegram_notifier import TelegramNotifier
from .delivery import binding_status


def _profile(config: dict) -> tuple:
    settings = (config.get("notification") or {}).get("telegram") or {}
    return (
        settings.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", ""),
        settings.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", ""),
        settings.get("parse_mode", "HTML"),
    )


class TriggerTelegramNotifier(TelegramNotifier):
    """Reuse complete report builders and recheck before each HTTP submission."""

    def __init__(self, config: dict):
        super().__init__(deepcopy(config))
        self.binding = self.config.get("_trigger_context") or {}
        self._telegram_profile = _profile(self.config)

    def _delivery_guard(self) -> dict | None:
        if not channel_enabled(self.config, "telegram"):
            return {"status": "skipped", "reason": "channel_disabled"}
        state = binding_status(self.config)
        if state != "allowed":
            return {
                "status": "skipped" if state == "revoked" else "failed",
                "reason": "trigger_revoked"
                if state == "revoked"
                else "trigger_binding_invalid",
            }
        try:
            current = ConfigStore(Path(self.binding["config_path"])).load_runtime()
            if runtime_path(current).resolve() != runtime_path(self.config).resolve():
                return {"status": "failed", "reason": "trigger_profile_changed"}
            if not channel_enabled(current, "telegram"):
                return {"status": "skipped", "reason": "channel_disabled"}
            access = BotAccess(current, "feishu", allow_wildcard=True)
            if not access.enabled or not access.gate.is_allowed(
                self.binding.get("chat_id")
            ):
                return {"status": "skipped", "reason": "trigger_chat_unauthorized"}
            if _profile(current) != self._telegram_profile:
                return {
                    "status": "failed",
                    "reason": "trigger_telegram_profile_changed",
                }
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            yaml.YAMLError,
        ):
            return {"status": "failed", "reason": "trigger_configuration_unavailable"}
        return None

    def send_from_session(self, session):
        guard = self._delivery_guard()
        return guard if guard else super().send_from_session(session)

    def send_daily_report_from_session(self, session):
        guard = self._delivery_guard()
        return guard if guard else super().send_daily_report_from_session(session)

    def send_brief_report(self, session, report_config):
        guard = self._delivery_guard()
        return guard if guard else super().send_brief_report(session, report_config)

    def _send_classified(self, title: str, body: str) -> dict:
        reason = self.binding.get("reason")
        if reason:
            body = html.escape(str(reason)) + "\n\n" + body
        return super()._send_classified(title, body)
