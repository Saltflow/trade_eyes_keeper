"""
Telegram 通知器 — Bot API 消息推送（HTML 格式）
简报：逐行独立小段 + emoji 着色
日报：价格/基本面/技术指标三分段
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import requests

from .base import BaseNotifier
from .receipts import combine_receipts, legacy_tuple, normalize_receipt
from .report_policy import (
    build_return_curves_text,
    fundamental_text,
)
from .settings import channel_enabled

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# emoji 着色
UP = "\U0001f7e2"  # 🟢 上涨
DOWN = "\U0001f534"  # 🔴 下跌
FLAT = "\u26aa"  # ⚪ 持平/无数据


def _redact_transport_error(error: Exception, bot_token: str) -> str:
    """Return a log-safe transport error without Telegram credentials."""
    message = str(error)
    if bot_token:
        message = message.replace(bot_token, "<redacted>")
    return re.sub(r"/bot[^/\s]+/", "/bot<redacted>/", message)


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 通知器，通过 sendMessage API 推送 HTML 格式消息"""

    def __init__(self, config: dict):
        self.config = config
        tc = config.get("notification", {}).get("telegram", {})
        self.bot_token = tc.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = tc.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
        self.parse_mode = tc.get("parse_mode", "HTML")
        self._enabled = bool(self.bot_token and self.chat_id)

    # ── 传输层 ──────────────────────────────────

    def _send(self, title: str, body: str, *, classified: bool = False):
        receipt = self._send_classified(title, body)
        return receipt if classified else legacy_tuple(receipt)

    def _send_classified(self, title: str, body: str) -> dict:
        if not channel_enabled(self.config, "telegram"):
            return {
                "status": "skipped",
                "reason": "channel_disabled",
                "detail": "Telegram 通知已禁用",
            }
        if not self.bot_token or not self.chat_id:
            return {
                "status": "failed",
                "reason": "configuration_missing",
                "detail": "Telegram bot_token 或 chat_id 未配置",
            }

        full_text = f"<b>{_esc(title)}</b>\n\n{body}"
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        MAX_LEN = 4000
        chunks = _split_text(full_text, MAX_LEN)

        pieces = []
        halted = False
        for i, chunk in enumerate(chunks):
            if halted:
                pieces.append(
                    {
                        "ordinal": i,
                        "status": "skipped",
                        "attempted": False,
                        "reason": "batch_stopped",
                    }
                )
                continue
            prefix = f"({i + 1}/{len(chunks)}) " if len(chunks) > 1 else ""
            try:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": self.chat_id,
                        "text": prefix + chunk,
                        "parse_mode": self.parse_mode,
                        "disable_web_page_preview": "true",
                    },
                    timeout=15,
                    allow_redirects=False,
                )
            except (
                requests.exceptions.ConnectTimeout,
                requests.exceptions.InvalidURL,
                requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidHeader,
            ) as exc:
                receipt = {
                    "status": "failed",
                    "reason": "http_pre_submit_failed",
                    "error": type(exc).__name__,
                    "detail": "Telegram 连接前失败",
                }
            except Exception as exc:  # noqa: BLE001 - request might have arrived
                receipt = {
                    "status": "unknown",
                    "reason": "http_submission_unknown",
                    "error": type(exc).__name__,
                    "detail": _redact_transport_error(exc, self.bot_token),
                }
            else:
                status = resp.status_code
                if status != 200:
                    rejected = status in {400, 401, 403, 404, 405, 413, 415, 422, 429}
                    receipt = {
                        "status": "failed" if rejected else "unknown",
                        "reason": "http_rejected"
                        if rejected
                        else "http_response_uncertain",
                        "http_status": status,
                        "detail": f"Telegram HTTP {status}",
                    }
                else:
                    try:
                        response = resp.json()
                    except (TypeError, ValueError):
                        response = None
                    ok = response.get("ok") if isinstance(response, dict) else None
                    if type(ok) is not bool:
                        receipt = {
                            "status": "unknown",
                            "reason": "http_acknowledgement_missing",
                        }
                    elif not ok:
                        receipt = {"status": "failed", "reason": "provider_rejected"}
                    else:
                        receipt = {"status": "sent", "reason": "provider_accepted"}
                        message = response.get("result")
                        if isinstance(message, dict) and isinstance(
                            message.get("message_id"), int
                        ):
                            receipt["message_id"] = message["message_id"]
            halted = receipt["status"] != "sent"
            pieces.append({**receipt, "ordinal": i, "attempted": True})
        return combine_receipts(pieces)

    # ── 接口实现 ───────────────────────────────

    def send_from_session(self, session) -> dict:
        return self._send_report(session)

    def send_daily_report_from_session(self, session) -> dict:
        return self._send_report(session)

    def _send_report(self, session) -> dict:
        try:
            df = session.get_all_dataframe()
            sections = [("价格与基本面", f"<pre>{_esc(fundamental_text(df))}</pre>")]
        except Exception as exc:  # noqa: BLE001 - no HTTP call has started
            return {
                "status": "failed",
                "reason": "telegram_report_preparation_failed",
                "error": type(exc).__name__,
            }
        title = f"股票日报 - {datetime.now().astimezone():%Y-%m-%d}"
        preparation_failures = []
        try:
            from .report_builders import build_daily_events_text

            events = build_daily_events_text(session, df)
            if events:
                sections.append(("事件速览", events))
        except Exception as exc:  # noqa: BLE001 - preparation before transport
            preparation_failures.append(
                {
                    "label": "事件速览",
                    "status": "failed",
                    "attempted": False,
                    "reason": "preparation_failed",
                    "error": type(exc).__name__,
                }
            )
        try:
            from .email_notifier import build_strategy_text_summary

            summary = build_strategy_text_summary(session, markdown=False)
            if summary:
                sections.append(("收益曲线", f"<pre>{_esc(summary)}</pre>"))
        except Exception as exc:  # noqa: BLE001 - retain other report sections
            preparation_failures.append(
                {
                    "status": "failed",
                    "label": "策略与信号",
                    "reason": "preparation_failed",
                    "error": type(exc).__name__,
                }
            )
        pieces = []
        halted = False
        for ordinal, (label, body) in enumerate(sections):
            if halted:
                receipt = {
                    "status": "skipped",
                    "reason": "batch_stopped",
                    "attempted": False,
                }
            else:
                receipt = normalize_receipt(
                    self._send(
                        title, f"{label}\n{body}" if label else body, classified=True
                    )
                )
                halted = receipt["status"] != "sent"
            pieces.append({**receipt, "ordinal": ordinal, "label": label})
        return combine_receipts([*pieces, *preparation_failures])

    def send_brief_report(self, session, report_config: dict) -> dict:
        try:
            title, body = self._prepare_brief_report(session, report_config)
        except Exception as exc:  # noqa: BLE001 - no transport before preparation
            return {
                "status": "failed",
                "reason": "telegram_brief_preparation_failed",
                "error": type(exc).__name__,
            }
        return normalize_receipt(self._send(title, body, classified=True))

    def _prepare_brief_report(self, session, report_config: dict):
        title = report_config.get("label", "简报")
        text = fundamental_text(session.get_all_dataframe())
        text += "\n\n" + build_return_curves_text(
            getattr(session, "strategy_return_curves", None)
        )
        return title, f"<pre>{_esc(text)}</pre>"

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "\u2705" if status == "SUCCESS" else "\u274c"
        title = f"{emoji} 部署通知"
        body = f"<b>状态</b>: {status}\n<b>版本</b>: {version}\n<b>概要</b>: {summary}"
        return self._send(title, body)

    # ── 构建方法 ─────────────────────────────


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_text(text: str, max_len: int) -> list:
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks
