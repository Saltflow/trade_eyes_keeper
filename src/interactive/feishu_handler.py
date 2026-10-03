"""Handle message events already authenticated by the official WebSocket SDK."""

import json
import logging

from .command_parser import parse_command
from .feishu_app import FeishuApp

logger = logging.getLogger(__name__)


def handle_feishu_message(app: FeishuApp, body: dict) -> str:
    """Accept SDK message events only; this is not an HTTP callback handler."""

    def finish(status: str, reason: str) -> str:
        # Record routing decisions without logging message text or identifiers.
        logger.info("Feishu message routed: status=%s reason=%s", status, reason)
        return status

    if not app.enabled:
        return finish("disabled", "bot_disabled")
    header = body.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return finish("ignored", "unsupported_event")
    event = body.get("event") or {}
    sender_type = (event.get("sender") or {}).get("sender_type")
    if sender_type and sender_type != "user":
        return finish("ignored", "non_user_sender")
    message = event.get("message") or {}
    chat_id = str(message.get("chat_id") or "")
    if not chat_id:
        return finish("ignored", "missing_chat")
    if not app.gate.is_allowed(chat_id):
        return finish("unauthorized", "chat_not_allowed")
    text = _message_text(message)
    if not text:
        return finish("ignored", "no_text")
    # Only a leading slash is a legacy command. A quoted example such as
    # '解释 /config set workers 4' must never execute that command.
    addressed = False
    mentions = message.get("mentions") or []
    if isinstance(mentions, list) and mentions:
        bot_id = app.get_bot_open_id()
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            identity = mention.get("id") or {}
            key = mention.get("key")
            if (
                bot_id
                and isinstance(identity, dict)
                and identity.get("open_id") == bot_id
                and isinstance(key, str)
                and key
                and key in text
            ):
                addressed = True
                text = text.replace(key, "").strip()
    is_command = text.startswith("/")
    if not is_command and (not app.assistant_enabled or not text):
        return finish("ignored", "assistant_disabled_or_empty_text")
    if not is_command and message.get("chat_type") != "p2p" and not addressed:
        return finish("ignored", "group_not_addressed")
    result = app.access.accept(
        chat_id, str(message.get("message_id") or header.get("event_id") or "")
    )
    if result != "accepted":
        if result == "rate_limited":
            app.send_message(chat_id, "操作过于频繁，请稍后再试。")
        return finish(result, "access_check")
    if is_command:
        command = parse_command(text)
        logger.info(
            "Feishu command: chat=%s command=%s", chat_id, command.cmd_type.name
        )
        app.command_executor.execute(chat_id, command)
        return finish("accepted", "legacy_command")
    sender_id = (event.get("sender") or {}).get("sender_id") or {}
    sender = str(sender_id.get("open_id") or "") if isinstance(sender_id, dict) else ""
    if not sender:
        return finish("ignored", "missing_sender")
    try:
        assistant = app.get_assistant()
        if assistant is not None:
            submitted = assistant.submit(
                chat_id,
                sender,
                text,
                str(message.get("message_id") or header.get("event_id") or ""),
            )
            return finish(
                "accepted", "assistant_queued" if submitted else "assistant_not_queued"
            )
    except Exception as exc:  # noqa: BLE001 - preserve legacy commands if assistant fails
        logger.error("Feishu assistant initialization failed: %s", type(exc).__name__)
        app.send_message(chat_id, "自然语言助手暂不可用；原有 /help 命令仍可使用。")
    return finish("accepted", "assistant_unavailable")


def _message_text(message: dict) -> str:
    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(content, dict):
        return ""
    text = content.get("text", "")
    if not text:
        # Feishu rich text posts are locale-keyed arrays of elements.
        posts = [content] if "content" in content else list(content.values())
        mention_keys = {}
        for mention in message.get("mentions") or []:
            if isinstance(mention, dict) and isinstance(mention.get("id"), dict):
                mention_keys[mention["id"].get("open_id")] = mention.get("key", "")
        paragraphs = []
        for post in posts:
            if not isinstance(post, dict):
                continue
            blocks = post.get("content", [])
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if isinstance(block, list):
                    paragraphs.append(
                        "".join(
                            str(element.get("text", ""))
                            if element.get("tag") != "at"
                            else str(
                                mention_keys.get(element.get("user_id"))
                                or element.get("user_id", "")
                            )
                            for element in block
                            if isinstance(element, dict)
                        )
                    )
        text = "\n".join(paragraphs)
    if not text:
        text = "".join(
            str(element.get("text", ""))
            for block in (content.get("blocks") or [])
            if isinstance(block, dict)
            for element in (block.get("elements") or [])
            if isinstance(element, dict)
        )
    if not isinstance(text, str):
        return ""
    return text.strip()
