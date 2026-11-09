"""
通知管理器 — 统一入口，多频道分发
"""

from __future__ import annotations

import logging
from importlib import import_module

from .settings import channel_enabled, env_flag

logger = logging.getLogger(__name__)


class NotifierManager:
    """按配置选择渠道，汇总每个渠道可验证的投递结果。"""

    def __init__(self, config: dict):
        self.trigger_delivery = "_trigger_context" in config
        self.email = None
        self.feishu = None
        self.telegram = None
        self._initialization_failures = {}
        if env_flag("SKIP_NOTIFICATIONS"):
            return
        channels = (
            (
                ("email", "..triggers.email_delivery", "TriggerEmailNotifier"),
                ("feishu", "..triggers.delivery", "TriggerChatNotifier"),
                ("telegram", "..triggers.telegram_delivery", "TriggerTelegramNotifier"),
            )
            if self.trigger_delivery
            else (
                ("email", ".email_notifier", "EmailNotifier"),
                ("feishu", ".feishu_notifier", "FeishuNotifier"),
                ("telegram", ".telegram_notifier", "TelegramNotifier"),
            )
        )
        for name, module, class_name in channels:
            if not channel_enabled(config, name):
                continue
            try:
                notifier = getattr(import_module(module, __package__), class_name)
                setattr(self, name, notifier(config))
            except Exception as exc:  # noqa: BLE001 - isolate channel initialization
                # No transport has started. Retain a failed channel instead of
                # silently treating it as disabled or blocking later channels.
                error = type(exc).__name__
                self._initialization_failures[name] = {
                    "status": "failed",
                    "reason": "channel_initialization_failed",
                    "error": error,
                }
                logger.error("频道 %s 初始化失败: %s", name, error)

    # ── 渠道列表 ────────────────────────────

    def _channels(self):
        ch = [self.email, self.feishu, self.telegram]
        return [c for c in ch if c is not None]

    # ── 分发方法 ────────────────────────────

    def _dispatch(self, method: str, *args) -> dict:
        """Return receipts, never infer delivery from a void transport return.

        A transport may acknowledge delivery with a structured result or its
        established ``(bool, detail)`` result. Merely returning without raising
        proves neither remote acceptance nor delivery.
        """
        deliveries = {
            name: dict(receipt)
            for name, receipt in getattr(self, "_initialization_failures", {}).items()
        }
        for name in ("email", "feishu", "telegram"):
            channel = getattr(self, name)
            if channel is None:
                continue
            try:
                result = getattr(channel, method)(*args)
                if isinstance(result, dict):
                    receipt = dict(result)
                    status = receipt.get("status")
                    if status in {"sent", "delivered", "succeeded"}:
                        receipt["status"] = "sent"
                    elif status not in {"failed", "partial", "unknown", "skipped"}:
                        receipt["status"] = "unknown"
                        receipt["reason"] = "transport_receipt_not_recognized"
                elif (
                    isinstance(result, tuple) and result and isinstance(result[0], bool)
                ):
                    receipt = {
                        "status": "sent" if result[0] else "failed",
                        "reason": str(result[1]) if len(result) > 1 else "",
                    }
                elif isinstance(result, bool):
                    receipt = {"status": "sent" if result else "failed"}
                else:
                    receipt = {
                        "status": "unknown",
                        "reason": "transport_acknowledgement_missing",
                    }
            except Exception as exc:
                logger.exception("频道 %s %s 失败", name, method)
                receipt = {
                    "status": "unknown",
                    "reason": "transport_exception_unclassified",
                    "error": type(exc).__name__,
                }
            deliveries[name] = receipt
        statuses = {item["status"] for item in deliveries.values()}
        if not statuses or statuses == {"skipped"}:
            status, reason = "skipped", "notifications_disabled"
        elif (
            "partial" in statuses
            or ("sent" in statuses and bool(statuses & {"failed", "unknown"}))
            or (self.trigger_delivery and "sent" in statuses and "skipped" in statuses)
        ):
            status, reason = "partial", "some_channels_not_confirmed"
        elif "unknown" in statuses:
            status, reason = "unknown", "delivery_result_unknown"
        elif "failed" in statuses:
            status, reason = "failed", "delivery_failed"
        else:
            status, reason = "succeeded", "delivery_acknowledged"
        return {"status": status, "reason": reason, "deliveries": deliveries}

    def send_from_session(self, session) -> dict:
        return self._dispatch("send_from_session", session)

    def send_daily_report_from_session(self, session) -> dict:
        return self._dispatch("send_daily_report_from_session", session)

    def send_brief_report(self, session, report_config: dict) -> dict:
        return self._dispatch("send_brief_report", session, report_config)

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> dict[str, bool]:
        results = dict.fromkeys(getattr(self, "_initialization_failures", {}), False)
        for name in ("email", "feishu", "telegram"):
            ch = getattr(self, name)
            if ch is None:
                continue
            try:
                result = ch.send_deployment_notification(status, version, summary)
                results[name] = bool(result and result[0])
            except Exception as e:  # noqa: BLE001 - isolate channel failure
                results[name] = False
                logger.error(
                    f"频道 {ch.__class__.__name__} send_deployment_notification 失败: {e}"
                )
        return results

    def send_test_email(self) -> tuple:
        """发送测试邮件（仅 Email 频道）"""
        if self.email:
            return self.email.send_test_email()
        if "email" in getattr(self, "_initialization_failures", {}):
            return False, "Email 频道初始化失败"
        return False, "Email 频道未启用"
