"""飞书事件处理器 — 接收事件、解析命令、执行、回复。"""

import json
import logging
import threading

from .command_parser import (
    AddCommand,
    AlertsCommand,
    BacktestCommand,
    BriefCommand,
    ConfigCommand,
    DailyCommand,
    ErrorCommand,
    HelpCommand,
    ListCommand,
    ModeCommand,
    OptimizeCommand,
    RefDateCommand,
    RemoveCommand,
    ResetAlertsCommand,
    ScheduleCommand,
    SaveCommand,
    SkipCommand,
    SwitchOptimizerCommand,
    parse_command,
)
from .commands.handlers import (
    handle_add,
    handle_alerts,
    handle_backtest,
    handle_brief,
    handle_config,
    handle_daily,
    handle_help,
    handle_list,
    handle_mode,
    handle_optimize,
    handle_ref_date,
    handle_remove,
    handle_reset_alerts,
    handle_save,
    handle_schedule,
    handle_skip,
    handle_switch_optimizer,
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

    # 去掉 @机器人 前缀 (如 "@试用clawbot /help" → "/help")
    text = text.strip()
    if text and not text.startswith("/"):
        # 尝试在文本中找第一个 "/" 作为命令起点
        slash_idx = text.find("/")
        if slash_idx >= 0:
            text = text[slash_idx:]

    cmd = parse_command(text)
    logger.info(f"飞书命令: chat={chat_id} text={text!r} cmd={cmd.cmd_type.name}")

    # 回测异步执行（避免 HTTP 超时吞结果）
    if isinstance(cmd, BacktestCommand):
        threading.Thread(
            target=_run_backtest_async,
            args=(app, chat_id, cmd),
            daemon=True,
        ).start()
        return 200, {"msg": "ok"}

    # 6. 执行 + 回复
    response = _dispatch(cmd)
    ok, msg = app.send_message(chat_id, response)
    if ok:
        logger.info(f"飞书回复成功: chat={chat_id} cmd={cmd.cmd_type.name} len={len(response)}")
    else:
        logger.error(f"飞书回复失败: chat={chat_id} msg={msg}")
    return 200, {"msg": "ok"}


def _dispatch(cmd) -> str:
    if isinstance(cmd, HelpCommand):
        return handle_help()
    if isinstance(cmd, ListCommand):
        return handle_list()
    if isinstance(cmd, AddCommand):
        return handle_add(cmd.codes)
    if isinstance(cmd, RemoveCommand):
        return handle_remove(cmd.codes)
    if isinstance(cmd, SaveCommand):
        return handle_save()
    if isinstance(cmd, BriefCommand):
        return handle_brief(cmd.report_id)
    if isinstance(cmd, OptimizeCommand):
        return handle_optimize(cmd.preset)
    if isinstance(cmd, DailyCommand):
        return handle_daily()
    if isinstance(cmd, ScheduleCommand):
        return handle_schedule(cmd.action, cmd.task_id, cmd.time_str)
    if isinstance(cmd, AlertsCommand):
        return handle_alerts()
    if isinstance(cmd, ResetAlertsCommand):
        return handle_reset_alerts(cmd.stock_code)
    if isinstance(cmd, ModeCommand):
        return handle_mode(cmd.mode)
    if isinstance(cmd, ConfigCommand):
        return handle_config(cmd.action, cmd.key, cmd.value)
    if isinstance(cmd, SkipCommand):
        return handle_skip(cmd.kind, cmd.codes, remove=cmd.remove)
    if isinstance(cmd, SwitchOptimizerCommand):
        return handle_switch_optimizer(cmd.kind)
    if isinstance(cmd, RefDateCommand):
        return handle_ref_date(cmd.date_str)
    if isinstance(cmd, ErrorCommand):
        return f"❌ {cmd.message}"
    return "❌ 未知命令。发送 /help 查看可用命令。"


def _run_backtest_async(app, chat_id, cmd):
    """后台线程执行回测，先发提示，跑完发结果。"""
    logger.info(f"异步回测开始: {cmd.stock_code} {cmd.start_date}~{cmd.end_date}")
    app.send_message(chat_id, f"⏳ 正在回测 <code>{cmd.stock_code}</code>…")
    result = handle_backtest(cmd.stock_code, cmd.start_date, cmd.end_date)
    ok, msg = app.send_message(chat_id, result)
    if ok:
        logger.info(f"异步回测完成: {cmd.stock_code} len={len(result)}")
    else:
        logger.error(f"异步回测发送失败: {cmd.stock_code} msg={msg}")
