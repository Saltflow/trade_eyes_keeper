"""Handle message events already authenticated by the official WebSocket SDK."""

import json
import logging

from .command_parser import parse_command
from .feishu_app import FeishuApp

logger = logging.getLogger(__name__)


def handle_feishu_message(app: FeishuApp, body: dict) -> str:
    """Accept SDK message events only; this is not an HTTP callback handler."""
    if not app.enabled:
        return "disabled"
    header = body.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return "ignored"
    event = body.get("event") or {}
    sender_type = (event.get("sender") or {}).get("sender_type")
    if sender_type and sender_type != "user":
        return "ignored"
    message = event.get("message") or {}
    chat_id = str(message.get("chat_id") or "")
    if not chat_id:
        return "ignored"
    result = app.access.accept(
        chat_id, str(message.get("message_id") or header.get("event_id") or "")
    )
    if result != "accepted":
        if result == "rate_limited":
            app.send_message(chat_id, "操作过于频繁，请稍后再试。")
        return result
    text = _message_text(message)
    if not text:
        return "ignored"
    command = parse_command(text)
    logger.info("Feishu command: chat=%s command=%s", chat_id, command.cmd_type.name)
    app.command_executor.execute(chat_id, command)
    return "accepted"


def _message_text(message: dict) -> str:
    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(content, dict):
        return ""
    text = content.get("text", "")
    if not text:
        text = "".join(
            str(element.get("text", ""))
            for block in content.get("blocks", [])
            for element in block.get("elements", [])
        )
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text.startswith("/"):
        slash = text.find("/")
        text = text[slash:] if slash >= 0 else ""
    return text
