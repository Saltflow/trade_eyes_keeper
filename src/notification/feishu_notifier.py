"""
飞书通知器 — 交互卡片消息推送
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests

from .base import BaseNotifier
from .receipts import combine_receipts, legacy_tuple, normalize_receipt
from .report_policy import (
    FUNDAMENTAL_COLUMNS,
    build_return_curves_text,
    fundamental_rows,
    quote_status_summary,
)
from .settings import channel_enabled

logger = logging.getLogger(__name__)


class FeishuNotifier(BaseNotifier):
    """飞书 Bot 通知器，通过 webhook 发送交互卡片消息"""

    def __init__(self, config: dict):
        self.config = config
        fc = config.get("notification", {}).get("feishu", {})
        self.webhook_url = fc.get("webhook_url") or os.getenv("FEISHU_WEBHOOK_URL", "")
        self.msg_type = fc.get("msg_type", "interactive")
        self._enabled = bool(self.webhook_url)

    # ── 传输层 ──────────────────────────────────

    def _send(
        self,
        title: str,
        body: str,
        extra_elements: list | None = None,
        *,
        classified: bool = False,
    ):
        """发送飞书卡片消息

        Args:
            title: 卡片标题
            body: 卡片正文（markdown 或纯文本）
            extra_elements: 追加的卡片 element（原生表格/分割线等）

        Returns:
            (bool, str): (是否成功, 消息)
        """
        try:
            card = _build_interactive_card(title, body, extra_elements=extra_elements)
        except Exception as exc:  # noqa: BLE001 - preparation precedes transport
            result = {
                "status": "failed",
                "reason": "feishu_card_preparation_failed",
                "error": type(exc).__name__,
                "detail": "飞书卡片准备失败",
            }
        else:
            result = normalize_receipt(
                self._send_card(
                    {"msg_type": "interactive", "card": card}, classified=True
                )
            )
        return result if classified else legacy_tuple(result)

    # ── 接口实现 ───────────────────────────────

    def send_from_session(self, session) -> dict:
        return self._send_report(session)

    def send_daily_report_from_session(self, session) -> dict:
        return self._send_report(session)

    def _send_report(self, session) -> dict:
        try:
            stock_data = session.get_all_dataframe()
            sections = [
                (
                    "价格与基本面",
                    "**完整监控股票池**\n" + quote_status_summary(stock_data),
                    [self._fundamental_table(stock_data)],
                )
            ]
        except Exception as exc:  # noqa: BLE001 - no HTTP call has started
            return {
                "status": "failed",
                "reason": "feishu_report_preparation_failed",
                "error": type(exc).__name__,
            }
        title = f"股票日报 - {datetime.now().astimezone():%Y-%m-%d}"
        preparation_failures = []
        try:
            from .report_builders import build_daily_events_text

            events = build_daily_events_text(session, stock_data)
            if events:
                sections.append(("事件速览", events, None))
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
            from ..notification.email_notifier import build_strategy_text_summary

            strat_body = build_strategy_text_summary(session, markdown=True)
            if strat_body:
                sections.append(("收益曲线", strat_body, None))
        except Exception as exc:  # noqa: BLE001 - retain other report sections
            preparation_failures.append(
                {
                    "label": "收益曲线",
                    "status": "failed",
                    "attempted": False,
                    "reason": "preparation_failed",
                    "error": type(exc).__name__,
                }
            )
        pieces = []
        halted = False
        for ordinal, (label, body, extra) in enumerate(sections):
            if halted:
                receipt = {
                    "status": "skipped",
                    "reason": "batch_stopped",
                    "attempted": False,
                }
            else:
                receipt = normalize_receipt(
                    self._send(
                        f"{title} · {label}",
                        body,
                        extra_elements=extra,
                        classified=True,
                    )
                )
                halted = receipt["status"] != "sent"
            pieces.append({**receipt, "ordinal": ordinal, "label": label})
        return combine_receipts([*pieces, *preparation_failures])

    def send_brief_report(self, session, report_config: dict) -> dict:
        try:
            payload = self._prepare_brief_report(session, report_config)
        except Exception as exc:  # noqa: BLE001 - no transport before preparation
            return {
                "status": "failed",
                "reason": "feishu_brief_preparation_failed",
                "error": type(exc).__name__,
            }
        receipt = normalize_receipt(self._send_card(payload, classified=True))
        return combine_receipts([{**receipt, "ordinal": 0, "label": "brief"}])

    def _prepare_brief_report(self, session, report_config: dict) -> dict:
        stock_data = session.get_all_dataframe()
        table = self._fundamental_table(stock_data)
        body = quote_status_summary(stock_data) + "\n\n" + build_return_curves_text(
            getattr(session, "strategy_return_curves", None)
        )
        card = _build_interactive_card(
            report_config.get("label", "简报"), body, extra_elements=[table]
        )
        return {"msg_type": "interactive", "card": card}

    def _send_card(self, payload: dict, *, classified: bool = False):
        """发送飞书卡片（不经过 _send 的 body 参数）"""
        result = self._post_card(payload)
        return result if classified else legacy_tuple(result)

    def _post_card(self, payload: dict) -> dict:
        if not channel_enabled(self.config, "feishu"):
            return {
                "status": "skipped",
                "reason": "channel_disabled",
                "detail": "飞书通知已禁用",
            }
        if not self.webhook_url:
            return {
                "status": "failed",
                "reason": "configuration_missing",
                "detail": "飞书 webhook_url 未配置",
            }
        try:
            json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError):
            return {"status": "failed", "reason": "payload_invalid"}
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json; charset=utf-8"},
                allow_redirects=False,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidHeader,
        ) as exc:
            return {
                "status": "failed",
                "reason": "http_pre_submit_failed",
                "error": type(exc).__name__,
                "detail": "飞书发送失败（连接前）",
            }
        except Exception as exc:  # noqa: BLE001 - submission might have arrived
            return {
                "status": "unknown",
                "reason": "http_submission_unknown",
                "error": type(exc).__name__,
                "detail": "飞书发送结果未知/timeout",
            }
        status = resp.status_code
        if status != 200:
            rejected = status in {400, 401, 403, 404, 405, 413, 415, 422, 429}
            return {
                "status": "failed" if rejected else "unknown",
                "reason": "http_rejected" if rejected else "http_response_uncertain",
                "http_status": status,
                "detail": f"飞书 HTTP {status}",
            }
        try:
            result = resp.json()
        except (TypeError, ValueError):
            result = None
        code = result.get("code") if isinstance(result, dict) else None
        if type(code) is not int:
            return {
                "status": "unknown",
                "reason": "http_acknowledgement_missing",
                "http_status": status,
            }
        return {
            "status": "sent" if code == 0 else "failed",
            "reason": "provider_accepted" if code == 0 else "provider_rejected",
            "provider_code": code,
            "http_status": status,
            "detail": "ok" if code == 0 else f"飞书 code={code}",
        }

    def send_deployment_notification(
        self, status: str, version: str = "", summary: str = ""
    ) -> tuple:
        emoji = "✅" if status == "SUCCESS" else "❌"
        title = f"{emoji} 部署通知"
        body = f"**状态**: {status}\n**版本**: {version}\n**概要**: {summary}"
        return self._send(title, body)

    # ── 辅助 ──────────────────────────────────

    @staticmethod
    def _fundamental_table(stock_data):
        columns = [
            (f"value{index}", label, "left", "text")
            for index, (_, label) in enumerate(FUNDAMENTAL_COLUMNS)
        ]
        rows = [
            {f"value{index}": value for index, value in enumerate(row)}
            for row in fundamental_rows(stock_data)
        ]
        return _build_native_table(columns, rows) or {
            "tag": "markdown",
            "content": "暂无监控数据",
        }


def _build_interactive_card(
    title: str, body: str, extra_elements: list[dict] | None = None
) -> dict:
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
