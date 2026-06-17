"""
通知管理器 — 统一入口，多频道分发
"""

import logging

from .base import BaseNotifier

logger = logging.getLogger(__name__)


class NotifierManager:
    """统一通知管理器：按 config 创建 enabled channel 的 notifier，并行分发"""

    def __init__(self, config: dict):
        nc = config.get("notification", {})

        # Email（默认启用，但如果完全没有 email 配置则跳过）
        ec = nc.get("email", {})
        email_enabled = ec.get("enabled", bool(ec))
        if email_enabled:
            from .email_notifier import EmailNotifier

            self.email = EmailNotifier(config)
        else:
            self.email = None

        # Feishu
        fc = nc.get("feishu", {})
        if fc.get("enabled", False):
            from .feishu_notifier import FeishuNotifier

            self.feishu = FeishuNotifier(config)
        else:
            self.feishu = None

        # Telegram
        tc = nc.get("telegram", {})
        if tc.get("enabled", False):
            from .telegram_notifier import TelegramNotifier

            self.telegram = TelegramNotifier(config)
        else:
            self.telegram = None

    # ── 渠道列表 ────────────────────────────

    def _channels(self):
        ch = [self.email, self.feishu, self.telegram]
        return [c for c in ch if c is not None]

    # ── 分发方法 ────────────────────────────

    def send_from_session(self, session) -> None:
        for ch in self._channels():
            try:
                ch.send_from_session(session)
            except Exception as e:
                logger.error(f"频道 {ch.__class__.__name__} send_from_session 失败: {e}")

    def send_daily_report_from_session(self, session) -> None:
        for ch in self._channels():
            try:
                ch.send_daily_report_from_session(session)
            except Exception as e:
                logger.error(f"频道 {ch.__class__.__name__} send_daily_report 失败: {e}")

    def send_brief_report(self, session, report_config: dict) -> None:
        for ch in self._channels():
            try:
                ch.send_brief_report(session, report_config)
            except Exception as e:
                logger.error(f"频道 {ch.__class__.__name__} send_brief_report 失败: {e}")

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> None:
        for ch in self._channels():
            try:
                ch.send_deployment_notification(status, version, summary)
            except Exception as e:
                logger.error(
                    f"频道 {ch.__class__.__name__} send_deployment_notification 失败: {e}"
                )

    def send_test_email(self) -> tuple:
        """发送测试邮件（仅 Email 频道）"""
        if self.email:
            return self.email.send_test_email()
        return False, "Email 频道未启用"

    def send_optimizer_notification(self, report, group_name: str = "") -> None:
        """分发优化结果到所有频道。"""
        for ch in self._channels():
            try:
                if hasattr(ch, "send_optimizer_notification"):
                    ch.send_optimizer_notification(report, group_name)
            except Exception as e:
                logger.error(
                    f"频道 {ch.__class__.__name__} send_optimizer_notification 失败: {e}"
                )
