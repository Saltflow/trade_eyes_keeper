"""Explicit acknowledgement adapter for existing notification transports."""

from __future__ import annotations

import html

from ..notification.settings import channel_enabled
from .email_report import render_email


def deliver_notification(
    config: dict,
    title: str,
    message: str,
    report: dict | None = None,
    *,
    manager=None,
    channels: list[str] | None = None,
    html_content: str | None = None,
) -> dict:
    """Report each channel outcome; disabled and empty delivery never mean success.

    The service owns event IDs and per-channel retry deduplication. ``channels``
    restricts retries to still-unacknowledged channels. ``report`` carries the
    current verified snapshot; local file paths are not advertised as public links.
    """
    selected = channels if channels is not None else ["email", "feishu", "telegram"]
    if (
        not config.get("fundamental_quant", {})
        .get("notifications", {})
        .get("enabled", False)
    ):
        return {
            "status": "skipped",
            "channels": {},
            "errors": ["fundamental notifications disabled"],
        }
    if manager is None:
        from ..notification.manager import NotifierManager

        try:
            manager = NotifierManager(config)
        except Exception as exc:  # noqa: BLE001 - transport boundary, explicit failure
            return {
                "status": "failed",
                "channels": {},
                "errors": [f"notifier initialization: {type(exc).__name__}"],
            }
    results = {}
    for name in selected:
        if name not in {"email", "feishu", "telegram"}:
            results[name] = {"status": "failed", "error": "unsupported channel"}
            continue
        channel = getattr(manager, name, None)
        if channel is None or not channel_enabled(config, name):
            results[name] = {"status": "skipped", "error": "channel disabled"}
            continue
        try:
            if name == "email":
                response = channel._send_email(
                    title, html_content if html_content is not None else render_email(title, message, report)
                )
                if isinstance(response, dict) and response.get("status") in {
                    "sent",
                    "skipped",
                    "failed",
                }:
                    results[name] = response
                else:
                    results[name] = {
                        "status": "failed",
                        "error": "email_transport_missing_acknowledgement",
                    }
                continue
            else:
                response = channel._send(
                    title, html.escape(message) if name == "telegram" else message
                )
                accepted = (
                    isinstance(response, tuple)
                    and bool(response)
                    and response[0] is True
                )
            results[name] = {
                "status": "sent" if accepted else "failed",
                "error": ""
                if accepted
                else "transport rejected or did not acknowledge delivery",
            }
        except Exception as exc:  # noqa: BLE001 - isolate failed transport
            # Transport errors may contain credentials; expose the type only.
            results[name] = {"status": "failed", "error": type(exc).__name__}
    statuses = [item["status"] for item in results.values()]
    status = (
        "failed"
        if "failed" in statuses
        else ("sent" if "sent" in statuses else "skipped")
    )
    return {
        "status": status,
        "channels": results,
        "errors": [
            f"{name}: {item['error']}"
            for name, item in results.items()
            if item["status"] == "failed"
        ],
    }
