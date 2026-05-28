"""
飞书通知器 — 交互卡片消息推送
"""

import json
import logging
import requests
from datetime import datetime

from .base import BaseNotifier

logger = logging.getLogger(__name__)


class FeishuNotifier(BaseNotifier):
    """飞书 Bot 通知器，通过 webhook 发送交互卡片消息"""

    def __init__(self, config: dict):
        fc = config.get("notification", {}).get("feishu", {})
        self.webhook_url = fc.get("webhook_url", "")
        self.msg_type = fc.get("msg_type", "interactive")
        self._enabled = bool(self.webhook_url)

    # ── 传输层 ──────────────────────────────────

    def _send(self, title: str, body: str) -> tuple:
        """发送飞书卡片消息

        Args:
            title: 卡片标题
            body: 卡片正文（markdown 或纯文本）

        Returns:
            (bool, str): (是否成功, 消息)
        """
        if not self.webhook_url:
            return False, "飞书 webhook_url 未配置"

        card = _build_interactive_card(title, body)
        payload = {"msg_type": "interactive", "card": card}
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            if resp.status_code != 200:
                logger.error(f"飞书 HTTP {resp.status_code}: {resp.text[:200]}")
                return False, f"飞书 HTTP {resp.status_code}"
            result = resp.json()
            code = result.get("code", -1)
            if code != 0:
                logger.error(f"飞书 API 错误 code={code}: {result.get('msg', '')}")
                return False, f"飞书 code={code} {result.get('msg', '')}"
            return True, "ok"
        except requests.exceptions.Timeout:
            logger.error("飞书请求超时")
            return False, "飞书请求超时"
        except Exception as e:
            logger.error(f"飞书发送失败: {e}")
            return False, str(e)

    # ── 接口实现 ───────────────────────────────

    def send_from_session(self, session) -> None:
        from .email_notifier import EmailNotifier
        email = EmailNotifier.__new__(EmailNotifier)
        email.__init__({"email": {}})  # dummy
        body = email._build_email_body(
            session.get_alerts_as_dicts(),
            session.get_all_dataframe(),
            announcements=getattr(session, "announcements", {}),
            signal_scan=getattr(session, "signal_scan", None),
            backtest=getattr(session, "backtest", None),
            portfolio_results=getattr(session, "portfolio_results", None),
        )
        title = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"
        self._send(title, _html_to_markdown(body))

    def send_daily_report_from_session(self, session) -> None:
        from .email_notifier import EmailNotifier
        email = EmailNotifier.__new__(EmailNotifier)
        email.__init__({"email": {}})
        body = email._build_email_body(
            [],
            session.get_all_dataframe(),
            announcements=getattr(session, "announcements", {}),
            signal_scan=getattr(session, "signal_scan", None),
            backtest=getattr(session, "backtest", None),
            portfolio_results=getattr(session, "portfolio_results", None),
        )
        title = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"
        self._send(title, _html_to_markdown(body))

    def send_brief_report(self, session, report_config: dict) -> None:
        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()
        rows = self._build_brief_rows(stock_data, today)
        title = f"{label} - {today.strftime('%Y-%m-%d')}"
        body = f"**{label}** | {today.strftime('%H:%M')}\n\n{rows}" if rows else f"**{label}** | 无活跃标的"
        self._send(title, body)

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "✅" if status == "SUCCESS" else "❌"
        title = f"{emoji} 部署通知"
        body = f"**状态**: {status}\n**版本**: {version}\n**概要**: {summary}"
        return self._send(title, body)

    # ── 辅助 ──────────────────────────────────

    @staticmethod
    def _build_brief_rows(stock_data, today) -> str:
        """构建简报表格行（Markdown）"""
        import pandas as pd
        from .email_notifier import EmailNotifier

        entries = []
        today_date = today.date()
        for _, row in stock_data.iterrows():
            data_date = row.get("date")
            in_trading = False
            if data_date is not None and not pd.isna(data_date):
                try:
                    d = pd.Timestamp(str(data_date)[:10]).date()
                    in_trading = 0 <= (today_date - d).days <= 3
                except Exception:
                    continue
            if not in_trading:
                continue
            close_price = row.get("close")
            anchors = {}
            for an in ("ma60", "wma20", "wma30", "wma50"):
                v = row.get(an)
                if v is not None and not pd.isna(v):
                    anchors[an] = float(v)
            best = None
            dev_pct = None
            if close_price is not None and not pd.isna(close_price) and anchors:
                best = EmailNotifier._pick_best_anchor(float(close_price), anchors)
            if best:
                _, _, dev_pct = best
            code = row.get("stock_code", "")
            close_str = f"{close_price:.2f}" if close_price is not None and not pd.isna(close_price) else "-"
            dev_str = f"{dev_pct:+.2f}%" if dev_pct is not None else "-"
            sort_key = dev_pct if dev_pct is not None else float("inf")
            entries.append((sort_key, code, close_str, dev_str))
        entries.sort(key=lambda x: x[0])
        if not entries:
            return ""
        lines = ["```", f"{'代码':<10} {'收盘':>8} {'偏离':>10}", "-" * 30]
        for _, code, close_str, dev_str in entries:
            lines.append(f"{code:<10} {close_str:>8} {dev_str:>10}")
        lines.append("```")
        return "\n".join(lines)


def _build_interactive_card(title: str, body: str) -> dict:
    """构建飞书交互卡片 JSON"""
    return {
        "header": {
            "title": {"tag": "plain_text", "content": title[:100]},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": body,
            }
        ],
    }


def _html_to_markdown(html: str) -> str:
    """HTML → 飞书卡片兼容的 Markdown（去标签 + 保留换行结构）"""
    import re
    text = html
    # 去掉 style 属性
    text = re.sub(r'\s*style="[^"]*"', "", text)
    # 基本转换
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    text = re.sub(r"</?strong>", "**", text)
    text = re.sub(r"</?b>", "**", text)
    text = re.sub(r"</?em>", "*", text)
    text = re.sub(r"</?h[1-6][^>]*>", "\n\n**", text)
    # 表格 → 紧凑代码块
    text = re.sub(r"</tr>", "\n", text)
    text = re.sub(r"<t[dh][^>]*>", " ", text)
    text = re.sub(r"</t[dh]>", "", text)
    # 去掉所有剩余 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 截断过长的卡片内容（飞书限制）
    if len(text) > 28000:
        text = text[:28000] + "\n\n... (内容过长已截断)"
    return text.strip()
