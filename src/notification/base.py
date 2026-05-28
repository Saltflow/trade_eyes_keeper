"""
通知基类
定义所有通知频道的统一接口
"""

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    """通知基类：Email / Feishu / Telegram 等频道继承此基类"""

    @abstractmethod
    def send_from_session(self, session) -> None:
        """发送完整告警邮件（从 Session 读取数据）"""
        ...

    @abstractmethod
    def send_daily_report_from_session(self, session) -> None:
        """发送每日报告（无告警时）"""
        ...

    @abstractmethod
    def send_brief_report(self, session, report_config: dict) -> None:
        """发送简报（仅价格 + 锚点偏离率）"""
        ...

    @abstractmethod
    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        """发送部署通知

        Returns:
            (bool, str): (是否成功, 消息)
        """
        ...
