"""
Telegram 通知器 — Bot API 消息推送（HTML 格式）
简报：逐行独立小段 + emoji 着色
日报：价格/基本面/技术指标三分段
"""

import logging
import os
import requests
import pandas as pd
from datetime import datetime

from .base import BaseNotifier

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# emoji 着色
UP = "\U0001f7e2"     # 🟢 上涨
DOWN = "\U0001f534"   # 🔴 下跌
FLAT = "\u26aa"       # ⚪ 持平/无数据


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 通知器，通过 sendMessage API 推送 HTML 格式消息"""

    def __init__(self, config: dict):
        tc = config.get("notification", {}).get("telegram", {})
        self.bot_token = tc.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = tc.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
        self.parse_mode = tc.get("parse_mode", "HTML")
        self._enabled = bool(self.bot_token and self.chat_id)

    # ── 传输层 ──────────────────────────────────

    def _send(self, title: str, body: str) -> tuple:
        if not self.bot_token or not self.chat_id:
            return False, "Telegram bot_token 或 chat_id 未配置"

        full_text = f"<b>{_esc(title)}</b>\n\n{body}"
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        MAX_LEN = 4000
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
        df = session.get_all_dataframe()
        alerts = session.get_alerts_as_dicts()
        today = datetime.now().strftime("%Y-%m-%d")
        title = f"股票提醒 - {today}"
        sections = self._build_daily_sections(df, alerts)
        for label, body in sections:
            self._send(title, f"{label}\n{body}")

    def send_daily_report_from_session(self, session) -> None:
        df = session.get_all_dataframe()
        today = datetime.now().strftime("%Y-%m-%d")
        title = f"股票日报 - {today}"
        sections = self._build_daily_sections(df)
        for label, body in sections:
            self._send(title, f"{label}\n{body}")

    def send_brief_report(self, session, report_config: dict) -> None:
        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()
        title = f"{label} - {today.strftime('%Y-%m-%d')}"
        body = self._build_brief_blocks(stock_data, today)
        if not body:
            body = "无活跃标的"
        self._send(title, body)

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "\u2705" if status == "SUCCESS" else "\u274c"
        title = f"{emoji} 部署通知"
        body = f"<b>状态</b>: {status}\n<b>版本</b>: {version}\n<b>概要</b>: {summary}"
        return self._send(title, body)

    # ── 构建方法 ─────────────────────────────

    @staticmethod
    def _build_brief_blocks(stock_data, today) -> str:
        """简报：每只标的独立小段，emoji 着色"""
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
            anchor_name = ""
            anchor_val = None
            if close_price is not None and not pd.isna(close_price) and anchors:
                best = EmailNotifier._pick_best_anchor(float(close_price), anchors)
            if best:
                anchor_name, anchor_val, dev_pct = best

            code = str(row.get("stock_code", ""))
            name = str(row.get("stock_name", code))
            close_str = f"{close_price:.2f}" if close_price is not None and not pd.isna(close_price) else "-"
            anchor_str = f"{anchor_val:.2f}" if anchor_val is not None else "-"

            if dev_pct is not None:
                emoji = UP if dev_pct > 0 else DOWN if dev_pct < 0 else FLAT
                dev_str = f"{dev_pct:+.2f}%"
            else:
                emoji = FLAT
                dev_str = "-"

            sort_key = dev_pct if dev_pct is not None else float("inf")
            entries.append((sort_key, code, name, close_str, anchor_name, anchor_str, dev_str, emoji))

        entries.sort(key=lambda x: x[0])
        if not entries:
            return ""

        lines = []
        for _, code, name, close_str, aname, aval, dev_str, emoji in entries:
            lines.append(
                f"<code>{code}</code> {name}\n"
                f"现价 {close_str}  {aname} {aval}  <b>{dev_str}</b>  {emoji}"
            )
        return "\n\n".join(lines)

    @staticmethod
    def _build_daily_sections(stock_data, alerts=None) -> list:
        """日报三分段: 价格+锚点 / 基本面 / 技术指标

        Returns:
            [(section_label, body), ...]
        """
        from .email_notifier import EmailNotifier
        today_date = datetime.now().date()

        # 过滤活跃标的
        entries = []
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
            anchor_name = ""
            if close_price is not None and not pd.isna(close_price) and anchors:
                best = EmailNotifier._pick_best_anchor(float(close_price), anchors)
            if best:
                anchor_name, _, dev_pct = best

            code = str(row.get("stock_code", ""))
            name = str(row.get("stock_name", code))
            close_str = f"{close_price:.2f}" if close_price is not None and not pd.isna(close_price) else "-"
            dev_str = f"{dev_pct:+.2f}%" if dev_pct is not None else "-"

            pe = _fmt(row.get("pe_ratio"), ".2f")
            pb = _fmt(row.get("pb_ratio"), ".2f")
            roe = _fmt(row.get("roe"), ".2f")
            div_yield = _fmt(row.get("dividend_yield"), ".2f")
            rsi = _fmt(row.get("rsi"), ".1f")
            macd_hist = _fmt(row.get("macd_hist"), ".3f")
            vol_ratio = _fmt(row.get("vol_ratio"), ".2f")
            boll_pct_b = _fmt(row.get("boll_pct_b"), ".2f")
            adx = _fmt(row.get("adx"), ".1f")

            sort_key = dev_pct if dev_pct is not None else float("inf")
            entries.append({
                "sk": sort_key, "code": code, "name": name,
                "close": close_str, "aname": anchor_name, "dev": dev_str,
                "pe": pe, "pb": pb, "roe": roe, "div_y": div_yield,
                "rsi": rsi, "macd_h": macd_hist, "vol_r": vol_ratio,
                "boll": boll_pct_b, "adx": adx,
            })

        entries.sort(key=lambda x: x["sk"])
        if not entries:
            return [("无活跃标的", "—")]

        sections = []

        # 第一段：价格 + 锚点偏离
        lines = ["<pre>"]
        lines.append(f"{'代码':<10} {'收盘':>7} {'锚点':>7} {'偏离':>8} {'名称'}")
        for e in entries:
            lines.append(f"{e['code']:<10} {e['close']:>7} {e['aname']:>7} {e['dev']:>8} {e['name'][:6]}")
        lines.append("</pre>")
        sections.append(("价格", "\n".join(lines)))

        # 第二段：基本面
        lines = ["<pre>"]
        lines.append(f"{'代码':<10} {'PE':>6} {'PB':>6} {'ROE%':>7} {'息%':>6}")
        for e in entries:
            lines.append(f"{e['code']:<10} {e['pe']:>6} {e['pb']:>6} {e['roe']:>7} {e['div_y']:>6}")
        lines.append("</pre>")
        sections.append(("基本面", "\n".join(lines)))

        # 第三段：技术指标
        lines = ["<pre>"]
        lines.append(f"{'代码':<10} {'RSI':>5} {'MACD_H':>8} {'VOL比':>6} {'ADX':>5} {'布林%':>7}")
        for e in entries:
            lines.append(f"{e['code']:<10} {e['rsi']:>5} {e['macd_h']:>8} {e['vol_r']:>6} {e['adx']:>5} {e['boll']:>7}")
        lines.append("</pre>")
        sections.append(("技术指标", "\n".join(lines)))

        return sections


def _fmt(v, spec=".2f"):
    """安全格式化 None/NaN → '-'"""
    if v is None:
        return "-"
    try:
        if pd.isna(v):
            return "-"
    except (TypeError, ValueError):
        pass
    try:
        return f"{float(v):{spec}}"
    except (ValueError, TypeError):
        return "-"


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
