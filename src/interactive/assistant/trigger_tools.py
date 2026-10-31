"""Authenticated trigger tools and narrow, deterministic command shortcuts.

The caller supplies the owner, reply chat, message identity and aware clock.
This module never sends messages or executes report jobs itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from pathlib import Path

from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
_ACTIONS = {"create", "update", "list", "get", "pause", "resume", "remove", "catalog"}
_ROUTE_KEYS = {"owner", "chat", "chat_id", "sender", "sender_id", "route"}
_PUBLIC_FAILURE_REASONS = {
    "http_rejected",
    "http_response_uncertain",
    "http_submission_unknown",
    "http_pre_submit_failed",
    "http_acknowledgement_missing",
    "feishu_rejected",
    "provider_rejected",
    "feishu_authentication_failed",
    "feishu_authentication_unavailable",
    "delivery_failed",
    "delivery_result_unknown",
    "some_pieces_not_confirmed",
    "payload_invalid",
    "feishu_card_preparation_failed",
    "feishu_report_preparation_failed",
    "feishu_brief_preparation_failed",
    "report_data_not_ready",
    "data_not_ready",
    "notification_receipt_missing",
    "report_worker_receipt_missing",
    "report_worker_receipt_exit_mismatch",
    "trigger_revoked",
    "channel_disabled",
    "trigger_binding_invalid",
    "trigger_configuration_unavailable",
    "configuration_missing",
}

TRIGGER_PROMPT = """
持久触发器使用trigger_manage；明确创建、暂停、恢复、删除直接执行，不要求多轮确认。
personal_monitor保留持仓及既有账户规则；新的通用条件/定时提醒使用trigger_manage，不创建为账户提醒。
先用catalog查询真实字段。条件支持递归all/any及field/op/value/max_age_seconds，禁止代码表达式。
数值遵循catalog.unit：percent_0_100字段的5%=5，ratio_0_1字段的5%=0.05；未知单位不能自行换算。
身份和回复位置由认证消息注入；不得在spec或payload指定owner、chat、sender或route。
价格条件使用quotes.<证券代码>.last；日报字段以catalog返回路径为准，未知字段说明缺项。
触发brief/daily时始终运行完整监控池；标的只作为条件、触发原因和高亮，不能缩成单股报告。
三季报事件使用announcement.q3_report、report_period=Q3、day_offsets=[0,1]，按监控池动态匹配。
提醒缺时刻默认Asia/Shanghai 09:50；回执必须给出完整年月日和时区。未给年份且今年日期已过时询问年份，不偷偷跨年。
编号是持久number，不是列表行号；先list/get取得真实状态，不猜编号或他人身份。
删除区分已删除、已不存在与已提交将尽力停止；正在投递或已送达可能无法撤回，未知投递不得声称已撤回。
只依据工具成功回执声称已创建或更改；复杂自然语言应转结构化规则，缺失条件明确询问。
list/get按action_results逐动作解释状态；last_result只兼容最新一条，不代表其他报告无记录。报告数据未就绪与是否投递成功分别说明。
""".strip()


def trigger_tool_schema() -> list[dict]:
    """One bounded management tool; the store validates the predicate grammar."""
    text = {"type": "string", "minLength": 1, "maxLength": 200}
    return [
        {
            "type": "function",
            "function": {
                "name": "trigger_manage",
                "description": "创建和查询持久触发器，或直接暂停、恢复、批量删除；先查询真实字段catalog。",
                "parameters": {
                    "type": "object",
                    "required": ["action"],
                    "additionalProperties": False,
                    "properties": {
                        "action": {"type": "string", "enum": sorted(_ACTIONS)},
                        "number": {"type": "integer", "minimum": 1},
                        "numbers": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "state": {
                            "type": "string",
                            "enum": ["active", "paused", "done", "deleted", "all"],
                        },
                        "prefix": {"type": "string", "maxLength": 200},
                        "expected_revision": {"type": "integer", "minimum": 1},
                        "changes": {
                            "type": "object",
                            "description": "先get读取当前revision，再提交spec顶层字段变更；不能改编号、身份或投递位置。",
                        },
                        "spec": {
                            "type": "object",
                            "required": ["label", "actions"],
                            "additionalProperties": False,
                            "properties": {
                                "label": text,
                                "condition": {
                                    "type": "object",
                                    "description": "安全递归谓词：all/any的子项，或field/op/value/max_age_seconds叶子；先catalog核对字段。",
                                },
                                "schedule": {
                                    "type": "object",
                                    "description": "at为带时区ISO时刻，或times为多个ISO时刻；timezone默认为Asia/Shanghai。",
                                },
                                "event": {
                                    "type": "object",
                                    "description": "kind、可选symbols/report_period及day_offsets；省略symbols表示监控池全部标的。",
                                },
                                "match_policy": {
                                    "type": "string",
                                    "enum": ["edge", "once", "level"],
                                },
                                "actions": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 10,
                                    "items": {
                                        "type": "object",
                                        "required": ["kind"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "kind": {
                                                "type": "string",
                                                "enum": ["brief", "daily", "reminder"],
                                            },
                                            "payload": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "properties": {
                                                    "text": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                        "maxLength": 2000,
                                                    },
                                                    "trigger_reason": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                        "maxLength": 2000,
                                                    },
                                                    "highlight_symbols": {
                                                        "type": "array",
                                                        "maxItems": 100,
                                                        "items": {
                                                            "type": "string",
                                                            "maxLength": 20,
                                                        },
                                                    },
                                                },
                                                "description": "提醒只用text；报告只用trigger_reason/highlight_symbols；身份及投递位置由认证会话注入。",
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    ]


def _reply(status: str, message: str, data=None) -> dict:
    return {
        "handled": True,
        "status": status,
        "message": message,
        "data": {} if data is None else data,
    }


def _clock(value: datetime | None) -> datetime:
    value = value if value is not None else datetime.now(BEIJING)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("触发器需要带时区的认证消息时间")
    return value.astimezone(BEIJING)


def _numbers(value) -> list[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("请提供1至100个触发器编号")
    if any(type(item) is not int or not 1 <= item <= 2**31 - 1 for item in value):
        raise ValueError("触发器编号必须是正整数")
    return list(dict.fromkeys(value))


def _clean(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            key: _clean(item)
            for key, item in value.items()
            if key
            not in _ROUTE_KEYS
            | {"run_id", "request_key", "action_id", "occurrence_key"}
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _untrusted_route(value, depth=0):
    if depth > 24:
        raise ValueError("规则嵌套过深，请简化规则")
    if isinstance(value, dict):
        return any(
            key in _ROUTE_KEYS or _untrusted_route(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_untrusted_route(item, depth + 1) for item in value)
    return False


def _chinese_number(value: str) -> int:
    if value.isascii() and value.isdigit():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "廿" in value:
        return 20 + (digits[value[-1]] if len(value) > 1 else 0)
    if "卅" in value:
        return 30 + (digits[value[-1]] if len(value) > 1 else 0)
    if "十" in value:
        left, right = value.split("十")
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return int("".join(str(digits[char]) for char in value))


class TriggerTools:
    """Bind every store operation to trusted message identity, never tool args."""

    def __init__(
        self,
        store,
        owner: str,
        chat: str,
        *,
        message_id: str = "",
        now: datetime | None = None,
        catalog=None,
    ):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("authenticated trigger owner is required")
        if not isinstance(chat, str) or not chat.strip():
            raise ValueError("authenticated trigger chat is required")
        self.store, self.owner, self.chat = store, owner, chat
        self.message_id, self.now, self.catalog = message_id, now, catalog

    def _catalog(self):
        if self.catalog is None:
            from ...triggers.catalog import build_report_field_catalog

            self.catalog = build_report_field_catalog()
        return self.catalog

    def _check_fields(self, predicate, depth=0):
        if depth > 16:
            raise ValueError("条件组合过深，请简化规则")
        if not isinstance(predicate, dict):
            raise TypeError("条件必须是all/any组合或field/op/value对象")
        for key in ("all", "any"):
            if key in predicate:
                if (
                    not isinstance(predicate[key], list)
                    or not 1 <= len(predicate[key]) <= 100
                ):
                    raise ValueError(f"{key}需要非空条件列表")
                for child in predicate[key]:
                    self._check_fields(child, depth + 1)
        if "field" in predicate and self._catalog().resolve(predicate["field"]) is None:
            raise ValueError(
                "未知日报/行情字段：" + str(predicate["field"]) + "；请先查询catalog"
            )
        if "not" in predicate:
            self._check_fields(predicate["not"], depth + 1)

    def _create(self, spec: dict, now: datetime, message_id: str) -> dict:
        if not isinstance(spec, dict) or _untrusted_route(spec):
            raise ValueError("规则不能指定owner/chat/sender/route；使用当前认证会话")
        if not message_id:
            raise ValueError("当前消息缺少可验证上下文，未创建触发器")
        value = self._prepare(spec)
        raw = json.dumps(
            spec,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        key = hashlib.sha256(
            (self.owner + "\0" + message_id + "\0" + raw).encode()
        ).hexdigest()
        created = self.store.create(self.owner, value, request_key=key, now=now)
        return self._saved_reply("created", created)

    def _prepare(self, spec: dict) -> dict:
        if not isinstance(spec, dict) or _untrusted_route(spec):
            raise ValueError("规则不能指定owner/chat/sender/route；使用当前认证会话")
        value = deepcopy(spec)
        actions = value.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 10:
            raise ValueError("请提供1至10个brief、daily或reminder动作")
        for action in actions:
            if not isinstance(action, dict) or set(action) - {"kind", "payload"}:
                raise ValueError("动作仅允许kind和payload")
            if action.get("kind") not in {"brief", "daily", "reminder"}:
                raise ValueError("动作必须是brief、daily或reminder")
            payload = action.setdefault("payload", {})
            if not isinstance(payload, dict):
                raise TypeError("动作payload必须是对象")
            allowed = (
                {"text"}
                if action["kind"] == "reminder"
                else {"trigger_reason", "highlight_symbols"}
            )
            if set(payload) - allowed:
                raise ValueError(
                    "动作payload仅允许提醒text或报告trigger_reason/highlight_symbols；报告使用完整监控池"
                )
            for key in ("text", "trigger_reason"):
                if key in payload and (
                    not isinstance(payload[key], str)
                    or not 1 <= len(payload[key]) <= 2000
                ):
                    raise ValueError(f"{key}需要1至2000字的文本")
            if action["kind"] == "reminder" and not payload.get("text"):
                raise ValueError("提醒动作需要text内容")
            highlights = payload.get("highlight_symbols", [])
            if (
                not isinstance(highlights, list)
                or len(highlights) > 100
                or any(
                    not isinstance(code, str)
                    or not re.fullmatch(r"[A-Za-z0-9.^_-]{1,20}", code)
                    for code in highlights
                )
            ):
                raise ValueError("highlight_symbols需要证券代码列表")
            if action["kind"] in {"brief", "daily"}:
                payload.setdefault("trigger_reason", str(value.get("label", "")))
            payload["chat_id"] = self.chat
        from ...triggers.models import normalize_spec

        value = normalize_spec(value)
        if "condition" in value:
            self._check_fields(value["condition"])
        if (
            "event" in value
            and self._catalog().resolve("events." + value["event"]["kind"]) is None
        ):
            raise ValueError("未知公告事件种类；请先查询catalog")
        return value

    def _saved_reply(self, status, created):
        value = created["spec"]
        number = created["number"]
        label = created.get("label", value.get("label", ""))
        schedule = value.get("schedule", {})
        when = ""
        if schedule.get("kind") == "dates":
            dates = [
                datetime.fromisoformat(moment)
                .astimezone(BEIJING)
                .strftime("%Y年%m月%d日 %H:%M")
                for moment in schedule["times"]
            ]
            when = "，时间为" + "、".join(dates) + "（Asia/Shanghai）"
        elif schedule:
            days = (
                "每天"
                if schedule.get("weekdays") == list(range(7))
                else "每周"
                + "、".join("一二三四五六日"[day] for day in schedule["weekdays"])
            )
            when = f"，{days}{schedule['at']}（{schedule['timezone']}）"
        event = value.get("event", {})
        if event.get("kind") == "announcement.q3_report":
            period = event.get("report_period", "Q3")
            season = (
                f"{period[:4]}年三季报"
                if re.fullmatch(r"\d{4}Q3", period)
                else "每年三季报"
            )
            moments = []
            for offset in event["day_offsets"]:
                stamp = event.get("offset_times", {}).get(str(offset))
                if offset == 0:
                    stamp = stamp or event.get("notify_at")
                    moments.append("当天" + stamp if stamp else "确认后当天立即")
                else:
                    day = "次日" if offset == 1 else f"第{offset}天"
                    moments.append(day + (stamp or event["day_after_time"]))
            when += (
                f"，{season}披露"
                + "发送、".join(moments)
                + f"发送（{event['timezone']}，含周末）"
            )
        if "condition" in value:
            when += "，条件为" + self._condition_summary(value["condition"])
        return _reply(
            status,
            f"已保存触发器 {number}：{label}{when}。",
            {"trigger": _clean(created)},
        )

    @staticmethod
    def _editable_spec(record):
        """Strip injected routes only; comparison values are business data."""
        spec = deepcopy(record["spec"])
        for action in spec["actions"]:
            for key in _ROUTE_KEYS:
                action.get("payload", {}).pop(key, None)
        return spec

    def _condition_summary(self, node):
        for key, join in (("all", " 且 "), ("any", " 或 ")):
            if key in node:
                return (
                    "（"
                    + join.join(self._condition_summary(item) for item in node[key])
                    + "）"
                )
        if "not" in node:
            return "非" + self._condition_summary(node["not"])
        path = node["field"]
        names = {
            "last": "现价",
            "ma60": "60日均线",
            "pe_ratio": "市盈率",
            "dividend_yield": "股息率",
            "roe": "ROE",
        }
        parts = path.split(".")
        field = (
            parts[1] + names.get(parts[-1], "." + parts[-1])
            if len(parts) == 3 and parts[0] in {"quotes", "stocks_data"}
            else path
        )
        op = {
            "lt": "小于",
            "lte": "不高于",
            "gt": "大于",
            "gte": "不低于",
            "eq": "等于",
            "ne": "不等于",
            "in": "属于",
            "not_in": "不属于",
            "contains": "包含",
            "between": "位于区间",
            "exists": "存在",
            "truthy": "为真",
            "startswith": "开头为",
            "endswith": "结尾为",
        }.get(node["op"], node["op"])
        value = json.dumps(node["value"], ensure_ascii=False) if "value" in node else ""
        return (field + op + value)[:350]

    def _execution_result(self, action):
        """Read the shared run without creating stores, reconciling or waking jobs."""
        if not action.get("run_id"):
            return {}
        expected = (
            "triggers.reminder"
            if action["kind"] == "reminder"
            else "reports." + action["kind"]
        )
        try:
            uri = Path(self.store.path).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT status,stage,result,reason FROM execution_runs WHERE id=? AND job_id=?",
                    (action["run_id"], expected),
                ).fetchone()
            if row is None:
                return {}
            result = dict(row)
            result["result"] = json.loads(result["result"] or "{}")
            if not isinstance(result["result"], dict):
                result["result"] = {}
            return result
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return {}

    @staticmethod
    def _safe_delivery_diagnostics(value):
        public = []
        pending = [value]
        for _ in range(256):
            if not pending:
                break
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item[:100])
            elif isinstance(item, dict):
                for key, child in item.items():
                    if (
                        key in {"http_status", "code", "provider_code"}
                        and type(child) is int
                    ):
                        prefix = "HTTP " if key == "http_status" else "平台code="
                        public.append(prefix + str(child))
                    elif key == "reason" and isinstance(child, str):
                        public.extend(
                            part
                            for part in child.split(":")
                            if part in _PUBLIC_FAILURE_REASONS
                        )
                    elif isinstance(child, (dict, list)):
                        pending.append(child)
        return list(dict.fromkeys(public))[:12]

    def _action_result(self, action, index, kind):
        if action is None:
            return {
                "action_index": index,
                "kind": kind,
                "status": "not_triggered",
                "message": "尚未触发",
                "diagnostics": [],
            }
        run = self._execution_result(action)
        state = action["state"]
        result = run.get("result") or action.get("result") or {}
        run_status = run.get("status")
        deliveries = result.get("deliveries")
        channel = deliveries.get("feishu") if isinstance(deliveries, dict) else None
        delivered = [channel.get("status")] if isinstance(channel, dict) else []
        terminal = {
            "succeeded",
            "failed",
            "unknown",
            "partial",
            "cancelled",
            "skipped",
            "interrupted",
            "expired",
        }
        if run_status == "queued" and state == "submitted":
            status, message = "queued", "排队中"
        elif run_status == "running" and state == "submitted":
            stage = run.get("stage", "")
            if stage in {
                "brief_notifications",
                "daily_notifications",
                "trigger_message_starting",
            }:
                status, message = "delivering", "正在投递"
            else:
                status, message = (
                    "generating",
                    "正在生成" if kind != "reminder" else "正在准备提醒",
                )
        elif delivered and all(value == "sent" for value in delivered):
            status, message = "sent", "已送达"
        elif (
            "partial" in delivered
            and isinstance(channel.get("pieces"), list)
            and any(
                item.get("status") == "failed"
                for item in channel["pieces"]
                if isinstance(item, dict)
            )
            and not any(
                item.get("status") in {"unknown", "partial"}
                for item in channel["pieces"]
                if isinstance(item, dict)
            )
        ):
            status, message = "failed", "部分送达，其他发送失败"
        elif "unknown" in delivered or "partial" in delivered:
            status, message = (
                "unknown",
                "结果不明" if "sent" not in delivered else "部分送达，其他结果不明",
            )
        elif "failed" in delivered:
            status, message = (
                "failed",
                "失败" if "sent" not in delivered else "部分送达，其他发送失败",
            )
        elif state == "pending":
            status, message = "pending", "待发送"
        elif state == "submitting":
            status, message = "submitting", "正在提交"
        elif state == "submitted" and run_status not in terminal:
            status, message = "submitted", "已提交，等待执行状态回执"
        else:
            outcome = run_status if run_status in terminal else state
            status, message = {
                "failed": ("failed", "失败"),
                "uncertain": ("unknown", "结果不明"),
                "unknown": ("unknown", "结果不明"),
                "partial": ("unknown", "结果不明"),
                "cancelled": ("cancelled", "已取消"),
                "skipped": ("cancelled", "已取消"),
                "expired": ("failed", "执行已过期"),
                "interrupted": ("unknown", "执行中断，结果待核实"),
            }.get(outcome, ("completed", "已完成，缺少明确投递回执"))
        diagnostics = self._safe_delivery_diagnostics(
            {
                "reason": action.get("reason"),
                "result": result,
                "run_reason": {"reason": run.get("reason")},
            }
        )
        if "report_data_not_ready" in diagnostics or "data_not_ready" in diagnostics:
            message += "；报告数据未就绪"
        display = [
            item for item in diagnostics if item.startswith(("HTTP ", "平台code="))
        ]
        display.sort(key=lambda item: (not item.startswith("HTTP "), item))
        if display:
            message += "（" + "，".join(display) + "）"
        return {
            "action_index": index,
            "kind": kind,
            "status": status,
            "message": message,
            "diagnostics": diagnostics,
            "updated_at": action.get("updated_at"),
        }

    def _action_results(self, record):
        rows = self.store.actions(self.owner, record["number"])
        latest = {}
        for action in rows:
            index = action["action_index"]
            if (
                not 0 <= index < len(record["spec"]["actions"])
                or action["kind"] != record["spec"]["actions"][index]["kind"]
            ):
                continue
            key = (action["created_at"], action["updated_at"], action["id"])
            if index not in latest or key > latest[index][0]:
                latest[index] = (key, action)
        return [
            self._action_result(latest.get(index, (None, None))[1], index, spec["kind"])
            for index, spec in enumerate(record["spec"]["actions"])
        ]

    def _describe(self, record, now, detail=False):
        from ...triggers.schedule import next_due

        spec = record["spec"]
        state = {
            "active": "生效中",
            "paused": "已暂停",
            "done": "已结束",
            "deleted": "已删除",
        }.get(record["state"], "状态待核实")
        summary = []
        if "condition" in spec:
            summary.append(self._condition_summary(spec["condition"]))
        schedule = spec.get("schedule")
        next_label = "等待行情/日报数据匹配"
        if schedule:
            zone = ZoneInfo(schedule["timezone"])
            if schedule["kind"] == "dates":
                summary.append(
                    "、".join(
                        datetime.fromisoformat(value)
                        .astimezone(zone)
                        .strftime("%Y年%m月%d日 %H:%M")
                        for value in schedule["times"]
                    )
                    + f"（{zone.key}）"
                )
            else:
                days = (
                    "每天"
                    if schedule["weekdays"] == list(range(7))
                    else "每周"
                    + "、".join("一二三四五六日"[day] for day in schedule["weekdays"])
                )
                summary.append(days + schedule["at"] + f"（{zone.key}）")
            due = next_due(schedule, now)
            next_label = (
                due.astimezone(zone).strftime("%Y年%m月%d日 %H:%M") + f"（{zone.key}）"
                if due
                else "暂无后续时刻，等待本次执行结果"
            )
        if "event" in spec:
            event = spec["event"]
            period = event.get("report_period", "Q3")
            name = "每年三季报" if period == "Q3" else period + "报告"
            offsets = []
            for offset in event["day_offsets"]:
                clock = event.get("offset_times", {}).get(str(offset))
                offsets.append(
                    ("当天" + (clock or event.get("notify_at", "立即")))
                    if offset == 0
                    else ("次日" if offset == 1 else f"第{offset}天")
                    + (clock or event["day_after_time"])
                )
            summary.append(
                name + "披露" + "及".join(offsets) + f"（{event['timezone']}，含周末）"
            )
            next_label = "等待符合规则的正式公告；按原披露日计算"
        if record["state"] != "active":
            next_label = "已停止后续触发"
        kinds = {"brief": "简报（完整监控池）", "daily": "日报（完整监控池）"}
        actions = "、".join(
            kinds.get(item["kind"], "提醒：" + item["payload"].get("text", ""))
            for item in spec["actions"]
        )
        evaluation = self.store.evaluation_state(self.owner, record["number"])
        try:
            diagnostic = json.loads(evaluation.get("diagnostic", "{}"))
        except (TypeError, ValueError):
            diagnostic = {}
        truth = diagnostic.get("truth", "unknown")
        latest = record.get("last_result")
        action_results = self._action_results(record)
        if len(action_results) == 1:
            recent = action_results[0]["message"].replace("已送达", "已发送", 1)
        else:
            names = {"brief": "简报", "daily": "日报", "reminder": "提醒"}
            parts = []
            for item in action_results:
                name = names[item["kind"]]
                if sum(other["kind"] == item["kind"] for other in action_results) > 1:
                    name += f"（动作{item['action_index'] + 1}）"
                parts.append(name + "：" + item["message"])
            recent = "；".join(parts)
        readiness = {
            "true": "最近条件核验满足",
            "false": "最近条件核验未满足",
            "unknown": "等待数据，未知不视为条件未满足",
        }.get(truth, "等待数据")
        if "condition" not in spec:
            readiness = (
                "等待符合规则的公告证据" if "event" in spec else "按计划等待执行"
            )
        message = f"{record['number']}. {record['label']}【{state}】\n条件/计划：{'；'.join(summary)}\n动作：{actions}\n下一次：{next_label}\n最近结果：{recent}；{readiness}"
        data = _clean(record)
        data["action_results"] = action_results
        data["last_result"] = (
            {
                key: latest[key]
                for key in ("state", "kind", "updated_at")
                if key in latest
            }
            if latest
            else None
        )
        data["evaluation"] = {
            "truth": truth,
            "last_evaluated_at": evaluation.get("last_evaluated_at"),
            "last_fired_at": evaluation.get("last_fired_at"),
        }
        if detail:
            notes = []
            labels = {
                "stale": "源数据已过期",
                "future_observation": "源数据时点在未来",
                "missing_timestamp": "缺少源时间",
                "invalid_timestamp": "源时间无效",
                "incompatible_value": "字段值无法比较",
                "unavailable": "字段暂无数据",
            }
            for reason in diagnostic.get("reasons", [])[:10]:
                path, _, code = str(reason).rpartition(":")
                if self._catalog().resolve(path) is not None:
                    notes.append(path + "：" + labels.get(code, "数据尚未就绪"))
            if notes:
                message += "\n诊断：" + "；".join(notes)
            message += "\n规则版本：" + str(record["revision"])
            data["evaluation"]["notes"] = notes
        return message, data

    def execute(
        self, action, args: dict | None = None, *, now=None, message_id=None
    ) -> dict:
        """Accept execute(args), execute('create', args), or a tool-name dispatch."""
        try:
            if isinstance(action, dict):
                params = dict(action)
                action = params.pop("action", None)
            else:
                params = dict(args or {})
                if action == "trigger_manage":
                    action = params.pop("action", None)
            if action not in _ACTIONS:
                raise ValueError(
                    "未知触发器操作；可用create/update/list/get/pause/resume/remove/catalog"
                )
            allowed = {
                "create": {"spec"},
                "list": {"state"},
                "get": {"number"},
                "update": {"number", "changes", "expected_revision"},
                "pause": {"number", "numbers"},
                "resume": {"number", "numbers"},
                "remove": {"number", "numbers"},
                "catalog": {"prefix"},
            }[action]
            if set(params) - allowed:
                raise ValueError("触发器操作含未知参数；身份和会话不可通过工具指定")
            if action == "create":
                return self._create(
                    params.get("spec"),
                    _clock(now or self.now),
                    self.message_id if message_id is None else message_id,
                )
            if action == "update":
                number = _numbers(params.get("number"))[0]
                revision, changes = (
                    params.get("expected_revision"),
                    params.get("changes"),
                )
                if type(revision) is not int or revision < 1:
                    raise ValueError(
                        "修改需要get返回的expected_revision，避免覆盖并发修改"
                    )
                if (
                    not isinstance(changes, dict)
                    or not changes
                    or _untrusted_route(changes)
                ):
                    raise ValueError(
                        "changes需要非空规则字段对象，不能指定身份或投递位置"
                    )
                if set(changes) - {
                    "label",
                    "condition",
                    "schedule",
                    "event",
                    "actions",
                    "match_policy",
                    "cooldown_seconds",
                }:
                    raise ValueError("changes只允许修改公开规则字段")
                record = self.store.get(self.owner, number)
                if record is None or record["state"] == "deleted":
                    return _reply("not_found", f"触发器 {number} 不存在。")
                value = self._prepare({**self._editable_spec(record), **changes})
                # Pass only requested fields. A full normalized spec would
                # re-open a done schedule or move its route on a label edit.
                patch = {key: value.get(key) for key in changes}
                changed = self.store.update(
                    self.owner,
                    number,
                    patch,
                    expected_revision=revision,
                    now=_clock(now or self.now),
                )
                return self._saved_reply("updated", changed)
            if action == "catalog":
                catalog = self._catalog()
                described = catalog.describe_fields()
                prefix = params.get("prefix", "")
                if not isinstance(prefix, str):
                    raise ValueError("字段prefix需要文本")
                described = [
                    item for item in described if item["path"].startswith(prefix)
                ]
                return _reply(
                    "ok",
                    "可用触发字段如下；未知字段不能假装已创建。",
                    {"catalog": _clean(described)},
                )
            if action == "list":
                state = params.get("state", "active")
                if state not in {"active", "paused", "done", "deleted", "all"}:
                    raise ValueError("状态应为active、paused、done、deleted或all")
                records = self.store.list(
                    self.owner, include_inactive=state != "active"
                )
                records = [
                    record
                    for record in records
                    if state == "all" or record["state"] == state
                ]
                records = sorted(records, key=lambda record: record["number"])
                descriptions = [
                    self._describe(record, _clock(now or self.now))
                    for record in records
                ]
                message = (
                    "暂无符合条件的触发器。"
                    if not descriptions
                    else "触发器（编号固定）：\n"
                    + "\n\n".join(item[0] for item in descriptions)
                )
                return _reply(
                    "ok",
                    message,
                    {"triggers": [item[1] for item in descriptions], "state": state},
                )
            if action == "get":
                number = _numbers(params.get("number"))[0]
                record = self.store.get(self.owner, number)
                if record is None:
                    return _reply("not_found", f"触发器 {number} 不存在。")
                message, described = self._describe(
                    record, _clock(now or self.now), detail=True
                )
                return _reply("ok", message, {"trigger": described})
            if "number" in params and "numbers" in params:
                raise ValueError("number与numbers不能同时提供")
            numbers = _numbers(params.get("numbers", params.get("number")))
            if action == "remove":
                result = self.store.remove(self.owner, numbers)
                parts = []
                for field, label in (
                    ("removed", "已删除"),
                    ("already_deleted", "已不存在"),
                    ("not_found", "不存在"),
                ):
                    if result.get(field):
                        parts.append(
                            label + "触发器 " + "、".join(map(str, result[field]))
                        )
                submitted = int(result.get("submitted_actions", 0))
                uncertain = int(result.get("uncertain_actions", 0))
                if submitted:
                    parts.append(
                        f"另有{submitted}个动作已提交，将尽力停止；正在投递或已送达可能无法撤回"
                    )
                if uncertain:
                    parts.append(f"{uncertain}个动作投递结果不确定，不能声称已撤回")
                return _reply("removed", "；".join(parts) + "。", _clean(result))
            results = []
            for number in numbers:
                record = self.store.get(self.owner, number)
                if record is None or record.get("state") == "deleted":
                    results.append({"number": number, "status": "not_found"})
                    continue
                changed = self.store.set_state(
                    self.owner, number, "paused" if action == "pause" else "active"
                )
                results.append({"number": number, "status": changed["state"]})
            label = "暂停" if action == "pause" else "恢复"
            message = "；".join(
                f"触发器 {row['number']}"
                + ("不存在" if row["status"] == "not_found" else "已" + label)
                for row in results
            )
            return _reply(
                "updated",
                message + "。已提交动作将尽力停止，正在投递或已送达可能无法撤回。",
                {"results": results},
            )
        except (ValueError, TypeError, KeyError) as exc:
            message = (
                str(exc)
                .replace(self.owner, "[当前用户]")
                .replace(self.chat, "[当前会话]")
            )
            return _reply(
                "needs_clarification",
                "未确认变更：" + message[:500],
                {"reason": message[:500]},
            )

    def handle_text(self, text: str, *, now=None, message_id=None) -> dict | None:
        """Handle explicit CRUD and unambiguous examples; leave complex prose to tools."""
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return None
        normalized = unicodedata.normalize("NFKC", text).strip().rstrip("。.!！")
        if re.fullmatch(
            r"(?:拉取|列出|查看|显示)(?:已经|已)?(?:激活|启用|生效)的?触发器|list\s+active\s+triggers?",
            normalized,
            re.IGNORECASE,
        ):
            return self.execute({"action": "list"})
        state_list = re.fullmatch(
            r"(?:拉取|列出|查看|显示)(?:已)?(暂停|完成|结束|删除)的?触发器", normalized
        )
        if state_list:
            state = {
                "暂停": "paused",
                "完成": "done",
                "结束": "done",
                "删除": "deleted",
            }[state_list[1]]
            return self.execute({"action": "list", "state": state})
        if re.fullmatch(
            r"(?:拉取|列出|查看|显示)(?:全部|所有)触发器|list\s+all\s+triggers?",
            normalized,
            re.IGNORECASE,
        ):
            return self.execute({"action": "list", "state": "all"})
        if re.fullmatch(
            r"(?:拉取|列出|查看|显示)触发器|list\s+triggers?", normalized, re.IGNORECASE
        ):
            return self.execute({"action": "list"})
        management = re.fullmatch(
            r"(remove|delete|pause|resume|删除|移除|取消|暂停|恢复|启用)\s*(?:(?:triggers?|触发器)\s*#?\s*|#?\s*(?=\d))(.*)",
            normalized,
            re.IGNORECASE,
        )
        if management:
            operation, raw = management.groups()
            if not re.fullmatch(r"\s*\d+(?:\s*[,、\s]\s*\d+)*\s*", raw):
                return _reply(
                    "needs_clarification",
                    "请提供触发器编号，例如 remove trigger 1,2,3；未执行更改。",
                )
            numbers = [int(value) for value in re.findall(r"\d+", raw)]
            action = (
                "remove"
                if operation.lower() in {"remove", "delete", "删除", "移除", "取消"}
                else "pause"
                if operation.lower() in {"pause", "暂停"}
                else "resume"
            )
            return self.execute({"action": action, "numbers": numbers})
        get = re.fullmatch(
            r"(?:get|查看|获取)\s*(?:trigger|触发器)\s*(\d+)", normalized, re.IGNORECASE
        )
        if get:
            return self.execute({"action": "get", "number": int(get[1])})
        update = re.fullmatch(
            r"(?:修改|把)\s*(?:触发器\s*)?#?\s*(\d+)\s*的?\s*(时间|价格阈值|阈值)\s*(?:改为|修改为|设为|为|到)\s*(.*)",
            normalized,
        )
        if update:
            number, field, new = int(update[1]), update[2], update[3]
            record = self.store.get(self.owner, number)
            if record is None or record.get("state") == "deleted":
                return _reply("not_found", f"触发器 {number} 不存在。")
            spec = record["spec"]
            changes = {}
            if field in {"阈值", "价格阈值"}:
                condition = spec.get("condition", {})
                if (
                    not re.fullmatch(r"\d+(?:\.\d+)?", new)
                    or not re.fullmatch(
                        r"quotes\.[A-Za-z0-9_-]+\.last", condition.get("field", "")
                    )
                    or condition.get("op") not in {"lt", "lte", "gt", "gte"}
                ):
                    return _reply(
                        "needs_clarification",
                        "请指定要修改的价格条件和数值；组合条件请先get后用结构化changes修改。",
                    )
                changes["condition"] = {**condition, "value": float(new)}
                changes["label"] = re.sub(
                    r"(低于|高于|不高于|不低于)\d+(?:\.\d+)?",
                    lambda match: match[1] + f"{float(new):g}",
                    spec["label"],
                )
            else:
                stamp = re.fullmatch(r"(\d{1,2})(?::|点)(\d{1,2})?分?", new)
                if not stamp:
                    return _reply(
                        "needs_clarification",
                        "请明确新的HH:MM时间；改日期请先get并提交完整schedule。",
                    )
                try:
                    clock = time(int(stamp[1]), int(stamp[2] or 0))
                    if "schedule" in spec:
                        schedule = deepcopy(spec["schedule"])
                        if schedule.get("kind") == "dates":
                            if len(schedule["times"]) != 1:
                                raise ValueError(
                                    "该触发器有多个日期，请明确要修改哪个日期"
                                )
                            old = datetime.fromisoformat(
                                schedule["times"][0]
                            ).astimezone(BEIJING)
                            moment = datetime.combine(old.date(), clock, BEIJING)
                            if moment <= _clock(now or self.now):
                                raise ValueError("新时间已经过去，请明确完整日期")
                            schedule["times"] = [moment.isoformat()]
                        else:
                            schedule["at"] = clock.strftime("%H:%M")
                        changes["schedule"] = schedule
                    else:
                        raise ValueError(
                            "该触发器没有单独定时；请明确公告当天或次日时刻"
                        )
                except ValueError as exc:
                    return _reply("needs_clarification", str(exc) + "；未修改。")
            return self.execute(
                {
                    "action": "update",
                    "number": number,
                    "expected_revision": record["revision"],
                    "changes": changes,
                },
                now=now,
            )
        number = r"[\d一二三四五六七八九十零〇两廿卅]{1,4}"
        reminder = re.fullmatch(
            rf"(?:提醒我(?:在)?)?(?:(?P<year>{number})年)?(?P<month>{number})月(?P<day>{number})(?:日|号)"
            r"(?:\s*(?P<hour>\d{1,2})(?::|点)(?P<minute>\d{1,2})?分?)?\s*(?P<label>(?:国债)?逆回购到期)(?:提醒我)?",
            normalized,
        )
        if reminder:
            try:
                current = _clock(now or self.now)
                year = (
                    _chinese_number(reminder["year"])
                    if reminder["year"]
                    else current.year
                )
                month, day = (
                    _chinese_number(reminder["month"]),
                    _chinese_number(reminder["day"]),
                )
                hour = int(reminder["hour"]) if reminder["hour"] else 9
                minute = int(reminder["minute"] or 0) if reminder["hour"] else 50
                moment = datetime.combine(
                    date(year, month, day), time(hour, minute), BEIJING
                )
                if moment <= current:
                    return _reply(
                        "needs_clarification",
                        "该日期/时刻已经过去，请明确年份或新的提醒时间；未创建触发器。",
                    )
                spec = {
                    "label": reminder["label"],
                    "schedule": {"at": moment.isoformat(), "timezone": "Asia/Shanghai"},
                    "match_policy": "once",
                    "actions": [
                        {"kind": "reminder", "payload": {"text": reminder["label"]}}
                    ],
                }
                return self.execute(
                    {"action": "create", "spec": spec},
                    now=current,
                    message_id=message_id,
                )
            except (ValueError, KeyError):
                return _reply(
                    "needs_clarification",
                    "日期或时刻无效，请明确完整年月日及时间；未创建触发器。",
                )
        if re.fullmatch(
            r"任何(?:一个)?监控(?:标的|股票)(?:发布|披露|发)(?:三季度报告|第三季度报告|三季报|Q3报告)的?(?:当天|当日)(?:和|及)(?:次日|第二天)(?:发送|推送|发)(?:简报和日报|日报和简报)",
            normalized,
            re.IGNORECASE,
        ):
            spec = {
                "label": "监控标的三季报披露当天和次日发送简报和日报",
                "event": {
                    "kind": "announcement.q3_report",
                    "report_period": "Q3",
                    "day_offsets": [0, 1],
                    "day_after_time": "09:50",
                    "timezone": "Asia/Shanghai",
                },
                "match_policy": "level",
                "actions": [
                    {"kind": "brief", "payload": {}},
                    {"kind": "daily", "payload": {}},
                ],
            }
            return self.execute(
                {"action": "create", "spec": spec}, now=now, message_id=message_id
            )
        price = re.fullmatch(
            r"(?P<code>\d{5,6}|[A-Za-z][A-Za-z0-9.^_-]{0,15})\s*(?:的股价)?(?:跌到|跌至)\s*(?P<value>\d+(?:\.\d+)?)\s*(?:元)?(?:以下|之下)(?:了)?(?:就)?(?:发|发送|推送)(?:一份)?简报",
            normalized,
            re.IGNORECASE,
        )
        if price:
            code, threshold = price["code"].upper(), float(price["value"])
            spec = {
                "label": f"{code}现价低于{threshold:g}时发送简报",
                "condition": {
                    "field": f"quotes.{code}.last",
                    "op": "lt",
                    "value": threshold,
                    "max_age_seconds": 120,
                },
                "match_policy": "edge",
                "actions": [
                    {"kind": "brief", "payload": {"highlight_symbols": [code]}}
                ],
            }
            return self.execute(
                {"action": "create", "spec": spec}, now=now, message_id=message_id
            )
        recurring = re.fullmatch(
            r"(每天|每日|每周[一二三四五六日天])\s*(\d{1,2})(?::|点)(\d{1,2})?分?\s*(提醒我|发|发送|推送)\s*(.+)",
            normalized,
        )
        if recurring:
            try:
                clock = time(int(recurring[2]), int(recurring[3] or 0)).strftime(
                    "%H:%M"
                )
            except ValueError:
                return _reply(
                    "needs_clarification", "时刻无效，请明确HH:MM；未创建触发器。"
                )
            cadence, body = recurring[1], recurring[5]
            schedule = {
                "kind": "daily" if cadence in {"每天", "每日"} else "weekly",
                "at": clock,
                "timezone": "Asia/Shanghai",
            }
            if cadence.startswith("每周"):
                schedule["weekdays"] = [
                    "一二三四五六日".index(cadence[-1].replace("天", "日"))
                ]
            report_kinds = {
                "简报": ["brief"],
                "日报": ["daily"],
                "简报和日报": ["brief", "daily"],
                "日报和简报": ["daily", "brief"],
            }
            actions = (
                [{"kind": kind, "payload": {}} for kind in report_kinds[body]]
                if recurring[4] != "提醒我" and body in report_kinds
                else [{"kind": "reminder", "payload": {"text": body}}]
            )
            return self.execute(
                {
                    "action": "create",
                    "spec": {
                        "label": normalized,
                        "schedule": schedule,
                        "match_policy": "level",
                        "actions": actions,
                    },
                },
                now=now,
                message_id=message_id,
            )
        return None
