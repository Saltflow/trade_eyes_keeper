"""Explicitly authorized trigger email, using the regular report and SMTP path."""

from __future__ import annotations

import html
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..core.config_store import ConfigStore
from ..core.report_execution import runtime_path
from ..interactive.security import BotAccess
from ..notification.email_notifier import EmailNotifier
from ..notification.settings import channel_enabled
from .delivery import binding_status

_PROFILE_DEFAULTS = {
    "smtp_server": "",
    "smtp_port": 465,
    "sender_email": "",
    "sender_password": "",
    "receiver_email": "",
    "enable_tls": False,
    "enable_ssl": True,
}


def trigger_email_route_allowed(config: dict) -> bool:
    """Only a trusted configuration can grant a sender/chat global email access."""
    binding = config.get("_trigger_context")
    notification = config.get("notification") or {}
    settings = notification.get("email") if isinstance(notification, dict) else None
    routes = (
        settings.get("trigger_report_routes") if isinstance(settings, dict) else None
    )
    if (
        not isinstance(binding, dict)
        or binding.get("kind") not in ("brief", "daily")
        or not isinstance(routes, list)
        or not routes
    ):
        return False
    for route in routes:
        if (
            not isinstance(route, dict)
            or set(route) != {"owner", "chat_id"}
            or any(
                not isinstance(value, str) or not value.strip() or "*" in value
                for value in route.values()
            )
        ):
            return False
    return any(
        route["owner"] == binding.get("owner")
        and route["chat_id"] == binding.get("chat_id")
        for route in routes
    )


def _profile(config):
    settings = config.get("email") or {}
    if not isinstance(settings, dict):
        raise TypeError("email profile must be an object")
    return tuple(
        settings.get(key, default) for key, default in _PROFILE_DEFAULTS.items()
    )


def _event_url(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
    except ValueError:
        return ""
    return html.escape(value, quote=True)


def _trigger_context_html(binding):
    escape = lambda value: html.escape(str(value or ""), quote=True)
    lines = [
        '<section class="trigger-context"><h2>触发报告</h2>',
        f"<p>{escape(binding.get('reason'))}</p>",
    ]
    events = binding.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            title = escape(event.get("title") or "公告")
            url = _event_url(event.get("url"))
            if url:
                title = f'<a href="{url}" rel="noopener noreferrer">{title}</a>'
            details = " · ".join(
                escape(event.get(key))
                for key in ("symbol", "report_period", "published_at", "source")
                if event.get(key)
            )
            if event.get("day_offset") is not None:
                details += " · " + escape(
                    "公告当天"
                    if event["day_offset"] == 0
                    else f"公告后第{event['day_offset']}天"
                )
            lines.append(f"<p><strong>{title}</strong><br>{details}</p>")
    lines.append("</section>")
    return "".join(lines)


class TriggerEmailNotifier(EmailNotifier):
    """Pin SMTP destinations and check revocation again immediately before DATA."""

    def __init__(self, config):
        super().__init__(deepcopy(config))
        self.binding = self.config.get("_trigger_context") or {}
        self._email_profile = _profile(self.config)

    def _delivery_guard(self):
        if not channel_enabled(self.config, "email"):
            return {"status": "skipped", "reason": "email_channel_disabled"}
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
            routed = {**current, "_trigger_context": self.binding}
            if not trigger_email_route_allowed(
                self.config
            ) or not trigger_email_route_allowed(routed):
                return {"status": "skipped", "reason": "trigger_email_unauthorized"}
            if not channel_enabled(current, "email"):
                return {"status": "skipped", "reason": "email_channel_disabled"}
            access = BotAccess(current, "feishu", allow_wildcard=True)
            if not access.enabled or not access.gate.is_allowed(
                self.binding.get("chat_id")
            ):
                return {"status": "skipped", "reason": "trigger_chat_unauthorized"}
            if _profile(current) != self._email_profile:
                return {"status": "failed", "reason": "trigger_email_profile_changed"}
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

    def _send_email(self, subject, body, *args, **kwargs):
        guard = self._delivery_guard()
        if guard:
            return guard
        prefix = "触发日报" if self.binding.get("kind") == "daily" else "触发简报"
        subject = f"{prefix} #{self.binding.get('number', '?')} · {subject}"
        context = _trigger_context_html(self.binding)
        match = re.search(r"<body\b[^>]*>", body, re.IGNORECASE)
        body = (
            body[: match.end()] + context + body[match.end() :]
            if match
            else context + body
        )
        return super()._send_email(subject, body, *args, **kwargs)
