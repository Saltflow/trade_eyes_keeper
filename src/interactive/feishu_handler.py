"""飞书事件处理器 — 接收事件、解析命令、执行、回复。"""

import json
import logging

from .command_parser import (
    AddCommand,
    BacktestCommand,
    ErrorCommand,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    parse_command,
)
from .commands.handlers import (
    handle_add,
    handle_backtest,
    handle_help,
    handle_list,
    handle_remove,
)
from .feishu_app import FeishuApp

logger = logging.getLogger(__name__)


def handle_feishu_event(app: FeishuApp, headers: dict, body: dict) -> tuple[int, dict]:
    """处理飞书事件回调。

    Returns:
        (status_code, response_body): HTTP 状态码和响应体。
    """
    # 1. URL 验证（challenge）
    verified = app.verify_event(body)
    if isinstance(verified, dict):
        return 200, verified

    # 2. 签名校验
    if not app.verify_signature(headers, body):
        logger.warning("飞书事件签名校验失败")
        return 403, {"msg": "signature verification failed"}

    # 3. 提取消息
    event = (body.get("event") or {})
    if not event:
        # 可能是 header.type 嵌套格式
        header = body.get("header", {})
        if header.get("event_type") == "im.message.receive_v1":
            event = body

    event_type = body.get("header", {}).get("event_type") or body.get(
        "type", ""
    )
    if "message" not in str(event_type):
        return 200, {"msg": "ignored"}

    message = event.get("event", {}).get("message") or event.get("message", {})
    chat_id = (
        message.get("chat_id")
        or event.get("event", {}).get("sender", {}).get("sender_id", {})
        .get("open_id", "")
    )

    if not chat_id:
        return 200, {"msg": "no chat_id"}

    # 4. 安全校验
    if not app._allow_all and app.gate and not app.gate.is_allowed(chat_id):
        logger.warning(f"未授权的 chat_id: {chat_id}")
        return 200, {"msg": "unauthorized"}

    if not app.rate_limiter.check(chat_id):
        app.send_message(chat_id, "操作过于频繁，请稍后再试。")
        return 200, {"msg": "rate_limited"}

    # 5. 解析命令 — 飞书 content 是 JSON 字符串, 需二次解析
    text = ""
    raw_content = message.get("content", "")
    if raw_content:
        try:
            parsed = json.loads(raw_content)
            # 简单文本消息: {"text": "/help"}
            if isinstance(parsed, dict):
                text = parsed.get("text", "")
            if not text and isinstance(parsed, dict):
                # 富文本消息: {"blocks": [{"elements": [{"text": "/help"}]}]}
                for block in parsed.get("blocks", []):
                    for element in block.get("elements", []):
                        text += element.get("text", "")
        except (json.JSONDecodeError, TypeError):
            pass
    if not text:
        text = message.get("text", "")

    cmd = parse_command(text)
    logger.info(f"飞书命令: chat={chat_id} text={text!r} cmd={cmd.cmd_type.name}")

    # 6. 执行 + 回复
    response = _dispatch(cmd)
    app.send_message(chat_id, response)
    return 200, {"msg": "ok"}


def _dispatch(cmd) -> str:
    if isinstance(cmd, HelpCommand):
        return handle_help()
    if isinstance(cmd, ListCommand):
        return handle_list()
    if isinstance(cmd, AddCommand):
        return handle_add(cmd.stock_code)
    if isinstance(cmd, RemoveCommand):
        return handle_remove(cmd.stock_code)
    if isinstance(cmd, BacktestCommand):
        return handle_backtest(cmd.stock_code, cmd.start_date, cmd.end_date)
    if isinstance(cmd, ErrorCommand):
        return f"❌ {cmd.message}"
    return "❌ 未知命令。发送 /help 查看可用命令。"
