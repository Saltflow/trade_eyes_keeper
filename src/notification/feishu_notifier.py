"""
飞书通知器 — 交互卡片消息推送
"""

import logging
import os
import unicodedata
from datetime import datetime

import pandas as pd
import requests

from .base import BaseNotifier

logger = logging.getLogger(__name__)


class FeishuNotifier(BaseNotifier):
    """飞书 Bot 通知器，通过 webhook 发送交互卡片消息"""

    def __init__(self, config: dict):
        fc = config.get("notification", {}).get("feishu", {})
        self.webhook_url = fc.get("webhook_url") or os.getenv("FEISHU_WEBHOOK_URL", "")
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
        stock_data = session.get_all_dataframe()
        alerts = session.get_alerts_as_dicts()
        title = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"
        for label, body in self._build_report_sections(
            stock_data, alerts=alerts, alert_only=True
        ):
            self._send(f"{title} · {label}", body)

    def send_daily_report_from_session(self, session) -> None:
        stock_data = session.get_all_dataframe()
        title = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"
        for label, body in self._build_report_sections(stock_data):
            self._send(f"{title} · {label}", body)

    def send_brief_report(self, session, report_config: dict) -> None:
        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()
        rows = self._build_brief_rows(stock_data, today)
        title = f"{label} - {today.strftime('%Y-%m-%d')}"

        # 策略建议
        from ..notification.email_notifier import build_strategy_suggestions
        sug = build_strategy_suggestions(stock_data, today)
        strat_text = ""
        if sug and sug["active_count"] > 0:
            strat_text = (
                f"\n\n**策略建议** ({sug['strategy_label']})\n"
                f"活跃信号: {sug['active_count']}/{sug['total_count']}\n"
                f"```\n代码     名称   现价    触发信号\n{sug['text_rows']}\n```"
            )

        body = (
            f"**{label}** | {today.strftime('%H:%M')}\n\n{rows}{strat_text}"
            if rows
            else f"**{label}** | 无活跃标的{strat_text}"
        )
        self._send(title, body)

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "✅" if status == "SUCCESS" else "❌"
        title = f"{emoji} 部署通知"
        body = f"**状态**: {status}\n**版本**: {version}\n**概要**: {summary}"
        return self._send(title, body)

    def send_optimizer_notification(self, report, group_name: str = "") -> None:
        from ..notification.email_notifier import build_optimizer_summary

        title = f"策略优化完成 · {group_name}" if group_name else "策略优化完成"
        body = build_optimizer_summary(report, group_name)
        self._send(title, body)

    # ── 辅助 ──────────────────────────────────

    @staticmethod
    def _build_report_sections(stock_data, alerts=None, alert_only: bool = False) -> list:
        """构建飞书日报分段，按价格/基本面/技术指标拆成多张卡片。"""
        today = datetime.now()
        all_entries = _collect_report_entries(stock_data, today)
        alert_codes = _extract_alert_codes(alerts or [])
        entries = all_entries

        if alert_only:
            entries = [e for e in all_entries if e["code"] in alert_codes]
            title = "**告警标的**"
            empty_text = "本次没有可展示的告警标的。"
        else:
            title = "**监控日报**"
            empty_text = "本次没有可展示的活跃标的。"

        summary = [f"{title} | {today.strftime('%Y-%m-%d %H:%M')}", ""]
        summary.append(f"活跃标的: **{len(entries)}**")
        if alert_codes:
            summary.append(f"告警标的: **{len(alert_codes)}**")

        if not entries:
            summary.extend(["", empty_text])
            return [("摘要", "\n".join(summary))]

        worst = min(entries, key=lambda x: x["dev_sort"])
        best = max(entries, key=lambda x: x["dev_sort"])
        summary.append(
            f"最大负偏离: `{worst['code']}` {worst['name']} {worst['dev']}"
        )
        summary.append(
            f"最大正偏离: `{best['code']}` {best['name']} {best['dev']}"
        )
        summary.extend(["", "**价格 / 锚点偏离**"])
        summary.append(
            _build_plain_table(
                entries,
                [
                    ("code", "代码"),
                    ("name", "名称"),
                    ("close", "收盘"),
                    ("anchor", "锚点"),
                    ("dev", "偏离"),
                ],
            )
        )

        sections = [("价格", "\n".join(summary))]

        fundamental = ["**基本面**", ""]
        fundamental.append(
            _build_plain_table(
                entries,
                [
                    ("code", "代码"),
                    ("div_y", "息%"),
                    ("pe", "PE"),
                    ("pb", "PB"),
                    ("roe", "ROE%"),
                ],
            )
        )
        sections.append(("基本面", "\n".join(fundamental)))

        if _has_technical_data(entries):
            technical = ["**技术指标**", ""]
            technical.append(
                _build_plain_table(
                    entries,
                    [
                        ("code", "代码"),
                        ("rsi", "RSI"),
                        ("macd_h", "MACD_H"),
                        ("vol_r", "VOL比"),
                        ("adx", "ADX"),
                        ("boll", "布林%"),
                    ],
                )
            )
            sections.append(("技术", "\n".join(technical)))

        return sections

    @staticmethod
    def _build_brief_rows(stock_data, today) -> str:
        """构建简报表格行，三端共享数据。"""
        from ..notification.email_notifier import build_brief_entries

        entries = build_brief_entries(stock_data, today)
        if not entries:
            return ""
        headers = ["代码", "名称", "收盘", "偏离"]
        rows = [
            [str(e["code"]), e["name"][:6], f"{e['close']:.2f}", e["dev_str"]]
            for e in entries
        ]
        widths = _calc_column_widths(headers, rows)
        lines = [_format_table_row(headers, widths)]
        lines.append(_format_table_row(["-" * width for width in widths], widths))
        for row in rows:
            lines.append(_format_table_row(row, widths))
        return "\n".join(lines)


def _collect_report_entries(stock_data, today) -> list[dict]:
    """从监控 DataFrame 提取飞书日报行，按锚点偏离升序排序。"""
    from .email_notifier import EmailNotifier

    entries = []
    today_date = today.date()
    for _, row in stock_data.iterrows():
        data_date = row.get("date")
        if data_date is None or pd.isna(data_date):
            continue
        date_value = pd.Timestamp(str(data_date)[:10]).date()
        if not 0 <= (today_date - date_value).days <= 3:
            continue

        close_price = row.get("close")
        anchors = {}
        for anchor_name in ("ma60", "wma20", "wma30", "wma50"):
            anchor_value = row.get(anchor_name)
            if anchor_value is not None and not pd.isna(anchor_value):
                anchors[anchor_name] = float(anchor_value)

        best_anchor = None
        dev_pct = None
        if close_price is not None and not pd.isna(close_price) and anchors:
            best_anchor = EmailNotifier._pick_best_anchor(float(close_price), anchors)
        if best_anchor:
            anchor_name, _, dev_pct = best_anchor
        else:
            anchor_name = "-"

        code = str(row.get("stock_code", ""))
        name = _short_text(str(row.get("stock_name", code)), 10)
        entries.append({
            "code": code,
            "name": name,
            "close": _fmt_num(close_price),
            "anchor": anchor_name,
            "dev": f"{dev_pct:+.2f}%" if dev_pct is not None else "-",
            "dev_sort": dev_pct if dev_pct is not None else float("inf"),
            "div_y": _fmt_num(row.get("dividend_yield")),
            "pe": _fmt_num(row.get("pe_ratio")),
            "pb": _fmt_num(row.get("pb_ratio")),
            "roe": _fmt_num(row.get("roe")),
            "rsi": _fmt_num(row.get("rsi"), ".1f"),
            "macd_h": _fmt_num(row.get("macd_hist"), ".3f"),
            "vol_r": _fmt_num(row.get("vol_ratio")),
            "adx": _fmt_num(row.get("adx"), ".1f"),
            "boll": _fmt_num(row.get("boll_pct_b")),
        })

    entries.sort(key=lambda x: x["dev_sort"])
    return entries


def _extract_alert_codes(alerts) -> set[str]:
    codes = set()
    for alert in alerts:
        code = alert.get("stock_code") or alert.get("code")
        if code:
            codes.add(str(code))
    return codes


def _has_technical_data(entries: list[dict]) -> bool:
    fields = ("rsi", "macd_h", "vol_r", "adx", "boll")
    return any(
        any(entry.get(field) not in (None, "-") for field in fields)
        for entry in entries
    )


def _build_plain_table(entries: list[dict], columns: list[tuple[str, str]]) -> str:
    """构建飞书可读表格。

    飞书群机器人卡片支持粗体/标题，但不渲染 Markdown 管道表格或
    fenced code block，因此这里直接输出空格对齐的纯文本表格。
    """
    rows = [
        [_escape_cell(str(entry.get(key, "-"))) for key, _ in columns]
        for entry in entries
    ]
    headers = [title for _, title in columns]
    widths = _calc_column_widths(headers, rows)

    lines = [_format_table_row(headers, widths)]
    lines.append(_format_table_row(["-" * width for width in widths], widths))
    for row in rows:
        lines.append(_format_table_row(row, widths))
    return "\n".join(lines)


def _calc_column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], _display_width(cell))
    return [min(max(width, 4), 14) for width in widths]


def _format_table_row(values: list[str], widths: list[int]) -> str:
    padded = [
        _pad_display_width(value, widths[idx])
        for idx, value in enumerate(values)
    ]
    return "  ".join(padded)


def _pad_display_width(text: str, width: int) -> str:
    shortened = _truncate_display_width(text, width)
    padding = max(width - _display_width(shortened), 0)
    return shortened + " " * padding


def _truncate_display_width(text: str, max_width: int) -> str:
    if _display_width(text) <= max_width:
        return text
    ellipsis = "…"
    target = max(max_width - _display_width(ellipsis), 1)
    result = ""
    used = 0
    for char in text:
        char_width = _char_display_width(char)
        if used + char_width > target:
            break
        result += char
        used += char_width
    return result + ellipsis


def _display_width(text: str) -> int:
    return sum(_char_display_width(char) for char in text)


def _char_display_width(char: str) -> int:
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 2
    return 1


def _fmt_num(value, spec=".2f") -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):{spec}}"


def _short_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _escape_cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


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
