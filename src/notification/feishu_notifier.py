"""
飞书通知器 — 交互卡片消息推送
"""

import logging
import os
import unicodedata
from datetime import datetime
from pathlib import Path

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

    def _send(self, title: str, body: str, extra_elements: list | None = None) -> tuple:
        """发送飞书卡片消息

        Args:
            title: 卡片标题
            body: 卡片正文（markdown 或纯文本）
            extra_elements: 追加的卡片 element（原生表格/分割线等）

        Returns:
            (bool, str): (是否成功, 消息)
        """
        if not self.webhook_url:
            return False, "飞书 webhook_url 未配置"

        card = _build_interactive_card(title, body, extra_elements=extra_elements)
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
        for label, body, extra in self._build_report_sections(
            stock_data, alerts=alerts, alert_only=True
        ):
            self._send(f"{title} · {label}", body, extra_elements=extra)

        # 搜参策略 + 今日信号 + 定增摘要（与日报对齐，告警模式也需展示）
        try:
            from ..notification.email_notifier import build_strategy_text_summary
            strat_body = build_strategy_text_summary(session, markdown=True)
            if strat_body:
                self._send(f"{title} · 策略与信号", strat_body)
        except Exception as e:
            logger.warning(f"飞书策略摘要发送失败 (非致命): {e}")

        # ── 参考持仓 ──
        ref_md = _build_ref_portfolio_markdown(session)
        if ref_md:
            self._send(f"{title} · 参考持仓", ref_md)

    def send_daily_report_from_session(self, session) -> None:
        stock_data = session.get_all_dataframe()
        title = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"
        for label, body, extra in self._build_report_sections(stock_data):
            self._send(f"{title} · {label}", body, extra_elements=extra)

        # 搜参策略 + 今日信号 + 定增摘要（与邮件对齐）
        try:
            from ..notification.email_notifier import build_strategy_text_summary
            strat_body = build_strategy_text_summary(session, markdown=True)
            if strat_body:
                self._send(f"{title} · 策略与信号", strat_body)
        except Exception as e:
            logger.warning(f"飞书策略摘要发送失败 (非致命): {e}")

        # ── 参考持仓 ──
        ref_md = _build_ref_portfolio_markdown(session)
        if ref_md:
            self._send(f"{title} · 参考持仓", ref_md)

    def send_brief_report(self, session, report_config: dict) -> None:
        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()

        from ..notification.email_notifier import build_brief_entries

        entries = build_brief_entries(stock_data, today)
        title = f"{label} - {today.strftime('%Y-%m-%d')}"
        active_count = len(entries)

        # 摘要行
        summary = f"**{label}** | {today.strftime('%H:%M')} | 活跃: {active_count}"

        # 策略信号（直接用 SignalScanner 结果，和日报一致）
        signal_scan = getattr(session, "signal_scan", None)

        # 组装卡片 elements
        extra = []
        table = _build_brief_table_element(entries)
        if table:
            extra.append(table)
        if signal_scan and signal_scan.alerts:
            from ..notification.email_notifier import (
                _build_signal_label_map, _readable_signal,
            )
            map_a = _build_signal_label_map("a_share")
            map_hk = _build_signal_label_map("hk") or _build_signal_label_map("non_a_share")
            map_us = _build_signal_label_map("us") or _build_signal_label_map("non_a_share")
            alert_lines = []
            for a in signal_scan.alerts[:8]:
                code = getattr(a, "stock_code", "?")
                raw = getattr(a, "rule_label", "?")
                readable = _readable_signal(code, raw, map_a, map_hk, map_us)
            alert_lines.append(f"  {code} {readable}")
                extra.append({
                    "tag": "markdown",
                    "content": f"**策略信号** ({len(signal_scan.alerts)} 条)\n"
                               + "\n".join(alert_lines),
                })

        # ── 参考持仓（无论有无信号都展示）──
        ref_md = _build_ref_portfolio_markdown(session)
        if ref_md:
            extra.append({"tag": "markdown", "content": ref_md})

        card = _build_interactive_card(title, summary, extra_elements=extra if extra else None)
        payload = {"msg_type": "interactive", "card": card}
        ok, msg = self._send_card(payload)
        if not ok:
            logger.error(f"飞书简报发送失败: {msg}")

    def _send_card(self, payload: dict) -> tuple:
        """发送飞书卡片（不经过 _send 的 body 参数）"""
        if not self.webhook_url:
            return False, "飞书 webhook_url 未配置"
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            if resp.status_code != 200:
                msg = f"飞书 HTTP {resp.status_code}: {resp.text[:200]}"
                logger.error(msg)
                return False, msg
            result = resp.json()
            code = result.get("code", -1)
            if code != 0:
                msg = f"飞书 API 错误 code={code}: {result.get('msg', '')}"
                logger.error(msg)
                return False, msg
            return True, "ok"
        except requests.exceptions.Timeout:
            msg = "飞书请求超时"
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = f"飞书发送失败: {e}"
            logger.error(msg)
            return False, msg

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "✅" if status == "SUCCESS" else "❌"
        title = f"{emoji} 部署通知"
        body = f"**状态**: {status}\n**版本**: {version}\n**概要**: {summary}"
        return self._send(title, body)

    def send_optimizer_notification(self, report, group_name: str = "",
                                     full_report: dict | None = None) -> None:
        from ..notification.email_notifier import build_optimizer_summary

        title = f"策略优化完成 · {group_name}" if group_name else "策略优化完成"
        body = build_optimizer_summary(report, group_name, full_report,
                                         include_charts=False)
        self._send(title, body)


    # ── 辅助 ──────────────────────────────────

    @staticmethod
    def _build_report_sections(stock_data, alerts=None, alert_only: bool = False) -> list:
        """构建飞书日报分段，按价格/基本面/技术指标拆成多张卡片。

        每段返回 (label, markdown_body, extra_elements)，
        表格使用飞书 schema 2.0 原生 table 组件。
        """
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

        summary = [f"{title} · {today.strftime('%Y-%m-%d %H:%M')}", ""]
        summary.append(f"活跃标的 **{len(entries)}**"
                       + (f" · 告警 **{len(alert_codes)}**" if alert_codes else ""))

        if not entries:
            summary.extend(["", empty_text])
            return [("摘要", "\n".join(summary), None)]

        worst = min(entries, key=lambda x: x["dev_sort"])
        best = max(entries, key=lambda x: x["dev_sort"])
        summary.append(
            f"最大跌 `{worst['code']}` {worst['name']} "
            + _color_md(worst["dev"], False)
        )
        summary.append(
            f"最大涨 `{best['code']}` {best['name']} "
            + _color_md(best["dev"], True)
        )

        # ── 价格段：原生表格（偏离红绿着色）──
        price_cols = [
            ("code", "代码", "left", "text"),
            ("name", "名称", "left", "text"),
            ("close", "收盘", "right", "text"),
            ("anchor", "锚点", "center", "text"),
            ("dev", "偏离", "right", "lark_md"),
        ]
        price_rows = [
            {
                "code": e["code"], "name": e["name"], "close": e["close"],
                "anchor": e["anchor"],
                "dev": _color_md(e["dev"], (e.get("dev_sort") or 0) >= 0),
            }
            for e in entries
        ]
        price_extra = [{"tag": "markdown", "content": "**价格 / 锚点偏离**"},
                       _build_native_table(price_cols, price_rows)]
        sections = [("价格", "\n".join(summary), price_extra)]

        # ── 基本面段 ──
        fund_cols = [
            ("code", "代码", "left", "text"),
            ("div_y", "息%", "right", "text"),
            ("pe", "PE", "right", "text"),
            ("pb", "PB", "right", "text"),
            ("roe", "ROE%", "right", "text"),
        ]
        fund_rows = [
            {"code": e["code"], "div_y": e["div_y"], "pe": e["pe"],
             "pb": e["pb"], "roe": e["roe"]}
            for e in entries
        ]
        fund_extra = [_build_native_table(fund_cols, fund_rows)]
        sections.append(("基本面", "**基本面**", fund_extra))

        # ── 技术段 ──
        if _has_technical_data(entries):
            tech_cols = [
                ("code", "代码", "left", "text"),
                ("rsi", "RSI", "right", "text"),
                ("macd_h", "MACD", "right", "text"),
                ("vol_r", "量比", "right", "text"),
                ("adx", "ADX", "right", "text"),
                ("boll", "布林%", "right", "text"),
            ]
            tech_rows = [
                {"code": e["code"], "rsi": e["rsi"], "macd_h": e["macd_h"],
                 "vol_r": e["vol_r"], "adx": e["adx"], "boll": e["boll"]}
                for e in entries
            ]
            tech_extra = [_build_native_table(tech_cols, tech_rows)]
            sections.append(("技术", "**技术指标**", tech_extra))

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


def _build_ref_portfolio_markdown(session) -> str:
    """构建参考持仓 Markdown 片段（飞书卡片 text 元素）。"""
    status = getattr(session, "ref_portfolio_status", None)
    if not status:
        return ""

    lines = [
        "**📊 参考持仓**",
        f"期初: {status['inception_date']} | 净值: {status['nav']:,.0f} | "
        f"回报: {status['nav_return_pct']:+.2f}% | 交易日: {status['trading_days']}",
    ]
    if status["holdings"]:
        for h in status["holdings"]:
            lines.append(
                f"  {h['code']} {h['shares']}股 × {h['price']:.2f} = "
                f"{h['market_value']:,.0f} (成本 {h['avg_cost']:.2f})"
            )
    else:
        lines.append("  📭 空仓")
    lines.append(f"现金: {status['cash']:,.2f}")
    return "\n".join(lines)


def _build_brief_table_element(entries: list[dict]) -> dict | None:
    """构建飞书原生 table 组件（偏离率红绿着色）。"""
    if not entries:
        return None
    columns = [
        ("code", "代码", "left", "text"),
        ("name", "名称", "left", "text"),
        ("price", "现价", "right", "text"),
        ("dev", "偏离", "right", "lark_md"),
    ]
    rows = [
        {
            "code": str(e["code"]),
            "name": str(e["name"]),
            "price": f"{e['close']:.2f}" if e.get("close") is not None else "-",
            "dev": _color_md(e["dev_str"], (e.get("dev_pct") or 0) >= 0),
        }
        for e in entries
    ]
    return _build_native_table(columns, rows)


def _build_brief_strategy_table(sug: dict) -> dict | None:
    """构建策略建议 table（有信号的标的前置）。"""
    if not sug or sug.get("active_count", 0) == 0:
        return None
    active_entries = [e for e in sug["entries"] if e["signals"]]
    if not active_entries:
        return None
    columns = [
        ("code", "代码", "left", "text"),
        ("name", "名称", "left", "text"),
        ("signal", "触发信号", "left", "lark_md"),
        ("price", "现价", "right", "text"),
    ]
    rows = [
        {
            "code": str(e["code"]),
            "name": str(e["name"]),
            "signal": _color_md(", ".join(e["signals"]) or "—", True),
            "price": f"{e['close']:.2f}" if e.get("close") is not None else "-",
        }
        for e in active_entries
    ]
    return _build_native_table(columns, rows)


def _build_interactive_card(title: str, body: str, extra_elements: list[dict] | None = None) -> dict:
    """构建飞书交互卡片 JSON（schema 2.0，支持原生 table 组件）。"""
    elements = []
    if body:
        elements.append({"tag": "markdown", "content": body})
    if extra_elements:
        elements.extend(extra_elements)
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title[:100]},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def _build_native_table(columns: list[tuple], rows: list[dict]) -> dict:
    """构建飞书 schema 2.0 原生 table 组件。

    Args:
        columns: [(name, display_name, align, data_type), ...]
                 align: "left"|"right"|"center"; data_type: "text"|"lark_md"
        rows: [{name: value, ...}, ...]，值均为字符串

    飞书原生 table 要求 schema 2.0，rows 为按列名索引的字典（非数组）。
    """
    return {
        "tag": "table",
        "header_style": {"bold": True},
        "columns": [
            {
                "name": name,
                "display_name": disp,
                "data_type": dtype,
                "horizontal_align": align,
            }
            for name, disp, align, dtype in columns
        ],
        "rows": rows,
    }


def _color_md(text: str, positive: bool) -> str:
    """偏离/涨跌着色：正=绿，负=红。"""
    color = "green" if positive else "red"
    return f"<font color='{color}'>{text}</font>"
