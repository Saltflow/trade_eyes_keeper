"""
通知管理器 — 统一入口，多频道分发
"""

from __future__ import annotations

import logging

from .settings import channel_enabled, env_flag

logger = logging.getLogger(__name__)


class NotifierManager:
    """按配置选择渠道，汇总每个渠道可验证的投递结果。"""

    def __init__(self, config: dict):
        if env_flag("SKIP_NOTIFICATIONS"):
            self.email = None
            self.feishu = None
            self.telegram = None
            return
        if channel_enabled(config, "email"):
            from .email_notifier import EmailNotifier

            self.email = EmailNotifier(config)
        else:
            self.email = None

        # Feishu
        if channel_enabled(config, "feishu"):
            from .feishu_notifier import FeishuNotifier

            self.feishu = FeishuNotifier(config)
        else:
            self.feishu = None

        # Telegram
        if channel_enabled(config, "telegram"):
            from .telegram_notifier import TelegramNotifier

            self.telegram = TelegramNotifier(config)
        else:
            self.telegram = None

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
        deliveries = {}
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
                    isinstance(result, tuple)
                    and result
                    and isinstance(result[0], bool)
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
                    "status": "unknown", "reason": "transport_exception_unclassified",
                    "error": type(exc).__name__,
                }
            deliveries[name] = receipt
        statuses = {item["status"] for item in deliveries.values()}
        if not statuses or statuses == {"skipped"}:
            status, reason = "skipped", "notifications_disabled"
        elif "partial" in statuses or (
            "sent" in statuses and bool(statuses & {"failed", "unknown"})
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
        results = {}
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
        return False, "Email 频道未启用"

    def send_optimizer_notification(
        self, report, group_name: str = "", full_report: dict | None = None
    ) -> None:
        """分发优化结果到所有频道。"""
        for ch in self._channels():
            try:
                if hasattr(ch, "send_optimizer_notification"):
                    ch.send_optimizer_notification(report, group_name, full_report)
            except Exception as e:  # noqa: BLE001 - isolate channel failure
                logger.error(
                    f"频道 {ch.__class__.__name__} send_optimizer_notification 失败: {e}"
                )
