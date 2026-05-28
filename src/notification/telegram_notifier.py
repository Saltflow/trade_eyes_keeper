"""
Telegram 通知器 — Bot API 消息推送（HTML 格式）
"""

import logging
import requests
from datetime import datetime

from .base import BaseNotifier

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 通知器，通过 sendMessage API 推送 HTML 格式消息"""

    def __init__(self, config: dict):
        tc = config.get("notification", {}).get("telegram", {})
        self.bot_token = tc.get("bot_token", "")
        self.chat_id = tc.get("chat_id", "")
        self.parse_mode = tc.get("parse_mode", "HTML")
        self._enabled = bool(self.bot_token and self.chat_id)

    # ── 传输层 ──────────────────────────────────

    def _send(self, title: str, body: str) -> tuple:
        """发送 Telegram 消息（支持长文本分片）

        Args:
            title: 消息标题（嵌入正文首行）
            body: 消息正文（HTML 格式）

        Returns:
            (bool, str): (是否成功, 消息)
        """
        if not self.bot_token or not self.chat_id:
            return False, "Telegram bot_token 或 chat_id 未配置"

        full_text = f"<b>{_esc_html(title)}</b>\n\n{body}"
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"

        # 分片发送（Telegram 单条消息上限 4096 字符）
        MAX_LEN = 4000  # 留余量
        chunks = _split_text(full_text, MAX_LEN)

        for i, chunk in enumerate(chunks):
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
                )
                if resp.status_code != 200:
                    logger.error(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
                    return False, f"Telegram HTTP {resp.status_code}"
                result = resp.json()
                if not result.get("ok", False):
                    logger.error(f"Telegram API error: {result.get('description', '')}")
                    return False, result.get("description", "unknown error")
            except requests.exceptions.Timeout:
                logger.error("Telegram 请求超时")
                return False, "Telegram 请求超时"
            except Exception as e:
                logger.error(f"Telegram 发送失败: {e}")
                return False, str(e)

        return True, "ok"

    # ── 接口实现 ───────────────────────────────

    def send_from_session(self, session) -> None:
        from .email_notifier import EmailNotifier
        email = EmailNotifier.__new__(EmailNotifier)
        email.__init__({"email": {}})
        body = email._build_email_body(
            session.get_alerts_as_dicts(),
            session.get_all_dataframe(),
            announcements=getattr(session, "announcements", {}),
            signal_scan=getattr(session, "signal_scan", None),
            backtest=getattr(session, "backtest", None),
            portfolio_results=getattr(session, "portfolio_results", None),
        )
        title = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"
        self._send(title, _html_compact(body))

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
        self._send(title, _html_compact(body))

    def send_brief_report(self, session, report_config: dict) -> None:
        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()
        title = f"{label} - {today.strftime('%Y-%m-%d')}"
        body = self._build_brief_html(stock_data, today)
        if not body:
            body = "无活跃标的"
        self._send(title, body)

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "&#9989;" if status == "SUCCESS" else "&#10060;"
        title = f"{emoji} 部署通知"
        body = f"<b>状态</b>: {status}\n<b>版本</b>: {version}\n<b>概要</b>: {summary}"
        return self._send(title, body)

    # ── 辅助 ──────────────────────────────────

    @staticmethod
    def _build_brief_html(stock_data, today) -> str:
        """构建简报 HTML 表格"""
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
            name = row.get("stock_name", code)
            close_str = f"{close_price:.2f}" if close_price is not None and not pd.isna(close_price) else "-"
            dev_str = f"{dev_pct:+.2f}%" if dev_pct is not None else "-"
            sort_key = dev_pct if dev_pct is not None else float("inf")
            entries.append((sort_key, code, name, close_str, dev_str))
        entries.sort(key=lambda x: x[0])
        if not entries:
            return ""
        lines = ["<pre>", f"{'代码':<10} {'名称':<8} {'收盘':>8} {'偏离':>10}"]
        for _, code, name, close_str, dev_str in entries:
            lines.append(f"{code:<10} {name:<8} {close_str:>8} {dev_str:>10}")
        lines.append("</pre>")
        return "\n".join(lines)


def _esc_html(text: str) -> str:
    """转义 HTML 特殊字符"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html_compact(html: str) -> str:
    """HTML → Telegram HTML 子集（保留结构，去除 style/class）"""
    import re
    text = html
    # 去掉 style / class 属性
    text = re.sub(r'\s*style="[^"]*"', "", text)
    text = re.sub(r'\s*class="[^"]*"', "", text)
    # 去掉 script / style 标签内容
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    # 表格 → 语义化
    text = text.replace("<table", "<pre><table").replace("</table>", "</table></pre>")
    text = re.sub(r"<thead>.*?</thead>", "", text, flags=re.DOTALL)
    # 保留 Telegram 支持的标签: <b> <i> <u> <s> <code> <pre> <a>
    # 移除不支持的标签但保留文本内容
    for tag in ["div", "span", "td", "th", "tr", "tbody", "img", "br", "hr", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "p", "body", "html", "head", "meta", "link", "title"]:
        text = re.sub(f"<{tag}[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(f"</{tag}>", "", text, flags=re.IGNORECASE)
    # 压缩空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    # 截断
    if len(text) > 35000:
        text = text[:35000] + "\n\n... (内容过长已截断)"
    return text.strip()


def _split_text(text: str, max_len: int) -> list:
    """按最大长度分片，尽量在换行处断开"""
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks
