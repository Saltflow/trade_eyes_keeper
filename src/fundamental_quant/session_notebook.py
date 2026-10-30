"""Expose paid research outputs without promoting drafts to reviewed reports.

The SQLite source is opened read-only. Request messages and private model reasoning
never enter the export; records retain their event identity instead of being merged.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .legacy_reporting import _atomic, _document

_PRIVATE = {
    "reasoning_content",
    "reasoning",
    "chain_of_thought",
    "internal_reasoning",
    "messages",
    "system",
    "system_prompt",
    "prompt",
    "api_key",
    "authorization",
    "credentials",
    "raw_assistant_content",
    "access_token",
    "secret",
}
_SECTIONS = {
    "record": "研究记录",
    "facts": "原文数据摘录（尚非核验事实）",
    "estimates": "估算与敏感性",
    "calculations": "计算提案（非代码核验值）",
    "conclusions": "六项判断",
    "issues": "独立审查意见",
    "coverage": "阅读覆盖",
    "tools": "下一步工具请求",
    "next": "后续研究安排",
    "action": "研究动作",
    "report_complete": "模型自报完成（不代表验收）",
}
_LABELS = {
    "summary": "本轮发现",
    "changes": "经营变化与解释",
    "observed_change": "发生了什么变化",
    "management_explanation": "管理层解释（待核对原文）",
    "assessment": "研究者判断",
    "corroboration": "支持证据",
    "unresolved": "仍未解决的问题",
    "next_check": "下一项区分性核查",
    "id": "记录 ID",
    "key": "结论",
    "metric": "指标",
    "value": "数值",
    "unit": "单位",
    "currency": "币种",
    "period_end": "财期",
    "quote": "原文摘录",
    "company_id": "公司",
    "page_refs": "原文页",
    "reason": "判断依据",
    "status": "模型判断状态",
    "verdict": "审查判断",
    "low": "下限",
    "base": "基准",
    "high": "上限",
    "method": "估算方法",
    "sensitivity": "敏感性",
    "counterevidence": "反证",
    "limitations": "限制",
    "invalidation": "失效条件",
    "assumptions": "假设",
    "fact_refs": "事实记录引用",
    "estimate_refs": "估算引用",
    "calculation_refs": "计算引用",
    "expression": "公式",
    "values": "计算输入",
    "severity": "问题级别",
    "affected_conclusions": "影响结论",
    "supersedes": "替代旧记录",
    "correction_reason": "更正原因",
    "normalized_earnings": "正常化盈利",
    "capital_efficiency": "资本效率",
    "normalized_cashflow": "正常化现金流",
    "implied_return": "隐含回报",
    "peer_thesis": "同行比较",
    "thesis_breaker": "投资逻辑与失效条件",
}
_USAGE_KEYS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


def _safe(value):
    if isinstance(value, dict):
        return {
            str(k): _safe(v) for k, v in value.items() if str(k).lower() not in _PRIVATE
        }
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _read_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {}


def _page_links(db, root, output):
    tables = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "pdf_session_pages" not in tables:
        return {}
    links = {}
    # Only registered PDFs/images inside the project's data tree may become links.
    allowed_root = root.parent.resolve()
    for row in db.execute("SELECT payload FROM pdf_session_pages"):
        page = _read_json(row[0])
        ref = page.get("source_ref")
        if not ref:
            continue
        for field in ("image_path", "document_path"):
            raw_path = page.get(field)
            if not raw_path:
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            try:
                path.relative_to(allowed_root)
            except ValueError:
                continue
            if not path.is_file() or path.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".pdf",
            }:
                continue
            href = quote(os.path.relpath(path, output).replace("\\", "/"), safe="/.")
            if field == "document_path":
                number = page.get("page")
                if not isinstance(number, int) or number < 1:
                    continue
                href += f"#page={number}"
            links[ref] = {
                "href": href,
                "page": page.get("page"),
                "period_end": page.get("period_end"),
                "issuer_id": page.get("issuer_id"),
            }
            break
    return links


def _render(value, links):
    if isinstance(value, dict):
        return (
            "<dl>"
            + "".join(
                f"<dt>{_esc(_LABELS.get(k, k))}</dt><dd>{_render(v, links)}</dd>"
                for k, v in value.items()
            )
            + "</dl>"
        )
    if isinstance(value, list):
        return (
            "<ul>" + "".join(f"<li>{_render(v, links)}</li>" for v in value) + "</ul>"
        )
    if isinstance(value, str) and value in links:
        return f'<a href="{_esc(links[value]["href"])}">{_esc(value)}</a>'
    if isinstance(value, str) and value.startswith("pdf:"):
        return (
            f'<span class="warn">{_esc(value)}（未找到可用注册原页，不可点击）</span>'
        )
    return _esc(_LABELS.get(value, value) if isinstance(value, str) else value)


def _research_history(records, links):
    """Keep earlier business discussion visible when later rounds repair numbers."""
    research = [
        e for e in records if e["role"] == "pdf_research" and e["content"].get("record")
    ]
    body = '<div class="card"><h2>按阅读批次查看研究记录</h2>'
    body += "<p>保留早期经营讨论及后续修正，不用最后一轮覆盖前文。以下是研究者输出，不等同于管理层原话或已核验原因；历史记录中的错误须结合后续审查阅读。</p>"
    for index, event in enumerate(research):
        body += (
            "<details"
            + (" open" if index == 0 else "")
            + "><summary>"
            + _esc(
                f"第 {index + 1} 次输出 · 事件 {event['event_id']} · {event['created_at']}"
            )
            + "</summary>"
            + _render(event["content"]["record"], links)
            + "</details>"
        )
    if not research:
        body += "<p>尚无研究记录，不能据此补造管理层解释。</p>"
    return body + "</div>"


def _usage(payload):
    raw = payload.get("usage") or {}
    cache = raw.get("provider_cache_usage") or {}
    return {
        "calls": 1,
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "cache_hit_tokens": cache.get("prompt_cache_hit_tokens"),
        "cache_miss_tokens": cache.get("prompt_cache_miss_tokens"),
    }


def _add_usage(target, value):
    for key in _USAGE_KEYS:
        target[key] = target.get(key, 0) + (value.get(key) or 0)
    unknown = value.get("unknown_usage_calls", 0)
    if any(value.get(key) is None for key in _USAGE_KEYS if key != "calls"):
        unknown += 1
    target["unknown_usage_calls"] = target.get("unknown_usage_calls", 0) + unknown


def _number(value):
    return "未知" if value is None else f"{value:,}"


def _issue_links(issues):
    return (
        "<ul>"
        + "".join(
            f'<li><a href="#event-{i["event_id"]}">审查事件 {i["event_id"]} · '
            f"{_esc(i['issue'].get('severity', '意见'))} · "
            f"{_esc(' / '.join(_LABELS.get(k, k) for k in i['issue'].get('affected_conclusions', [])))}</a></li>"
            for i in issues
        )
        + "</ul>"
    )


def _content(payload):
    value = payload.get("content")
    if not isinstance(value, dict):
        # A failed response can still contain valid structured output. Never expose
        # unparsed raw text, which may include protocol/private material.
        value = _read_json(payload.get("raw_assistant_content"))
    return _safe(value) if isinstance(value, dict) else {}


def _event_html(event, links, issues):
    content = event["content"]
    role = (
        "研究者输出 · 未通过完整发布核验"
        if event["role"] == "pdf_research"
        else "独立审查者输出 · 审查意见也可能有误"
    )
    body = f'<article id="event-{event["event_id"]}"><h3>事件 {event["event_id"]} · {role}</h3>'
    body += f"<p>{_esc(event['created_at'])} · 输入 {_number(event['usage']['input_tokens'])} / 输出 {_number(event['usage']['output_tokens'])} token</p>"
    if event.get("error"):
        body += (
            '<div class="warn">调用失败：' + _render(event["error"], links) + "</div>"
        )
    if not content:
        body += (
            "<p>本次响应未形成可解析的研究记录；已保留费用与错误，不生成替代结论。</p>"
        )
    for section, value in content.items():
        body += f"<h4>{_esc(_SECTIONS.get(section, section))}</h4>"
        if section == "conclusions" and isinstance(value, list):
            for item in value:
                body += '<div class="claim">' + _render(item, links)
                related = [
                    i
                    for i in issues
                    if isinstance(item, dict)
                    and item.get("key") in i["issue"].get("affected_conclusions", [])
                ]
                if related:
                    body += '<details class="warn"><summary>相关审查争议与限制（含历史版本，非已解决认证）</summary>'
                    body += _issue_links(related) + "</details>"
                body += "</div>"
        elif section in {"facts", "calculations", "estimates", "tools"} and isinstance(
            value, list
        ):
            body += f"<details><summary>展开全部 {len(value)} 条记录（保留 0 值、版本和引用）</summary>"
            for item in value:
                body += '<div class="claim">' + _render(item, links) + "</div>"
                if isinstance(item, dict) and section != "tools":
                    refs = set(item.get("page_refs", []))
                    related = [
                        i
                        for i in issues
                        if refs.intersection(i["issue"].get("page_refs", []))
                        or (
                            item.get("id")
                            and item["id"] in i["issue"].get("reason", "")
                        )
                    ]
                    if related:
                        body += '<details class="warn"><summary>该记录或同页证据存在审查意见（请勿直接采信）</summary>'
                        body += _issue_links(related) + "</details>"
            body += "</details>"
        else:
            body += _render(value, links)
    return body + "</article>"


def render_session_notebooks(root: Path) -> dict:
    """Render archived research/review outputs; never invoke models or publish."""
    root = Path(root).resolve()
    output = root / "research_notes"
    database = root / "research.sqlite3"
    result = {
        "index_path": str(output / "index.html"),
        "json_path": str(output / "index.json"),
        "sessions": [],
        "totals": {k: 0 for k in _USAGE_KEYS},
    }
    if not database.is_file():
        return {**result, "status": "database_missing"}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "pdf_research_sessions" not in tables:
            return {**result, "status": "sessions_missing"}
        sessions = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM pdf_research_sessions ORDER BY updated_at DESC"
            )
        ]
        events = (
            [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,payload,created_at FROM events WHERE kind IN ('model_request','model_response','model_error') ORDER BY id"
                )
            ]
            if "events" in tables
            else []
        )
        links = _page_links(db, root, output)
    requests, by_session = {}, {}
    for event in events:
        payload = _read_json(event["payload"])
        if event["kind"] == "model_request":
            requests[payload.get("request_hash")] = {
                k: payload.get(k) for k in ("session_id", "role", "request_hash")
            }
            continue
        request = requests.get(payload.get("request_hash"), {})
        if not request.get("session_id"):
            continue
        role = request.get("role")
        if role not in {"pdf_research", "pdf_review", "pdf_compact"}:
            continue
        record = {
            "event_id": event["id"],
            "created_at": event["created_at"],
            "role": role,
            "request_hash": payload.get("request_hash"),
            "usage": _usage(payload),
            "usage_known": payload.get("usage_known", False),
            "error": _safe(payload.get("error")),
            "content": _content(payload) if role != "pdf_compact" else {},
        }
        by_session.setdefault(request["session_id"], []).append(record)
    style = "<style>body{max-width:1150px;margin:32px auto;padding:0 22px;font:16px/1.7 sans-serif;color:#243447;background:#f3f6fa}article,.card{background:white;border:1px solid #d5dfeb;border-radius:12px;padding:22px;margin:20px 0}dl{display:grid;grid-template-columns:minmax(120px,20%) 1fr;gap:6px 16px}dt{font-weight:bold;color:#536477}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}dd dl{display:block}a{color:#075ea8;overflow-wrap:anywhere}.warn{background:#fff2d8;color:#774b00;padding:10px}.claim{border-top:1px solid #d5dfeb;padding:12px 0}summary{cursor:pointer;font-weight:bold}li{margin:8px 0}h2,h3{color:#173e64}</style>"
    intro = '<p class="warn">这是已有付费输出的研究笔记，不是正式候选或已验收报告。模型提取、估算及计算可能有误；审查意见逐项保留，不用未完成核验掩盖已经产出的内容。相同 ID 的多次输出按事件保留，不合并为事实。</p>'
    cards = []
    for row in sessions:
        state = _read_json(row["state"])
        sid = row["id"]
        filename = sid if sid.isalnum() else hashlib.sha256(sid.encode()).hexdigest()
        company = state.get("company", {})
        records = by_session.get(sid, [])
        public_records = [e for e in records if e["role"] != "pdf_compact"]
        issues = [
            {"event_id": e["event_id"], "issue": i}
            for e in public_records
            for i in e["content"].get("issues", [])
            if isinstance(i, dict)
        ]
        summary = {
            "id": sid,
            "company_id": row["company_id"],
            "name": company.get("name", row["company_id"]),
            "campaign_id": row.get("campaign_id"),
            "phase": state.get("phase"),
            "path": str(output / (filename + ".html")),
            "json_path": str(output / (filename + ".json")),
            "event_count": len(public_records),
            "usage": {k: 0 for k in _USAGE_KEYS},
            "usage_by_role": {},
        }
        for field, section in (
            ("fact_count", "facts"),
            ("estimate_count", "estimates"),
            ("calculation_count", "calculations"),
            ("conclusion_count", "conclusions"),
            ("review_issue_count", "issues"),
        ):
            summary[field] = sum(
                len(e["content"].get(section, [])) for e in public_records
            )
        for event in records:
            _add_usage(summary["usage"], event["usage"])
            _add_usage(
                summary["usage_by_role"].setdefault(event["role"], {}), event["usage"]
            )
        _add_usage(result["totals"], summary["usage"])
        heading = f"<h1>{_esc(summary['name'])} · 已有研究笔记</h1>"
        body = style + heading + '<p><a href="index.html">全部公司</a></p>' + intro
        research_history = _research_history(public_records, links)
        body += research_history
        body += (
            '<details class="card"><summary>已产生的内容与累计费用</summary><p>费用合计只加已知值；unknown_usage_calls 是存在未知用量的调用数。</p>'
            + _render(
                {
                    k: summary[k]
                    for k in (
                        "phase",
                        "event_count",
                        "fact_count",
                        "estimate_count",
                        "calculation_count",
                        "conclusion_count",
                        "review_issue_count",
                        "usage_by_role",
                    )
                },
                links,
            )
            + "</details>"
        )
        latest_facts = next(
            (
                e
                for e in reversed(public_records)
                if e["role"] == "pdf_research" and e["content"].get("facts")
            ),
            None,
        )
        numeric_preview = ""
        if latest_facts:
            numeric_preview = f'<details class="card"><summary>基础数值参考 · 事件 {latest_facts["event_id"]}</summary><p class="warn">模型原始提取，尚非核验事实。下方保留全部历史版本及审查争议。</p><table><tr><th>指标</th><th>值</th><th>单位／币种</th><th>财期</th><th>来源</th></tr>'
            preview_facts = [
                fact
                for fact in latest_facts["content"]["facts"]
                if fact.get("company_id", row["company_id"]) == row["company_id"]
            ]
            priority = (
                "营业收入",
                "归母",
                "归属于",
                "净利润",
                "经营活动现金流",
                "经营现金流",
                "revenue",
                "net_profit",
                "cfo",
            )
            preview_facts.sort(
                key=lambda fact: (
                    not any(
                        term in str(fact.get("metric", "")).lower() for term in priority
                    )
                )
            )
            for fact in preview_facts[:8]:
                numeric_preview += (
                    "<tr>"
                    + "".join(
                        "<td>" + _esc(v) + "</td>"
                        for v in (
                            fact.get("metric", fact.get("id")),
                            fact.get("value"),
                            str(fact.get("unit", ""))
                            + " / "
                            + str(fact.get("currency", "")),
                            fact.get("period_end", ""),
                        )
                    )
                    + "<td>"
                    + _render(fact.get("page_refs", []), links)
                    + "</td></tr>"
                )
            numeric_preview += "</table><p>此处仅预览最近一份含数据输出的本公司最多 8 条记录，优先展示收入、盈利、经营现金流；完整记录全部保留在时间线。</p></details>"
            body = body.replace(
                '<details class="card"><summary>已产生的内容与累计费用</summary>',
                numeric_preview
                + '<details class="card"><summary>已产生的内容与累计费用</summary>',
                1,
            )
        latest_conclusions = next(
            (
                e
                for e in reversed(public_records)
                if e["role"] == "pdf_research" and e["content"].get("conclusions")
            ),
            None,
        )
        if latest_conclusions:
            body += '<div class="card"><h2>最近六项判断（研究者原文）</h2>'
            for claim in latest_conclusions["content"]["conclusions"]:
                body += (
                    "<h3>"
                    + _esc(_LABELS.get(claim.get("key"), claim.get("key")))
                    + "</h3>"
                )
                body += _render(
                    {
                        key: claim[key]
                        for key in ("status", "reason", "limitations", "sensitivity")
                        if key in claim
                    },
                    links,
                )
                related = [
                    i
                    for i in issues
                    if claim.get("key") in i["issue"].get("affected_conclusions", [])
                ]
                if related:
                    body += (
                        '<details class="warn"><summary>查看相关审查争议</summary>'
                        + _issue_links(related)
                        + "</details>"
                    )
            body += "</div>"
        arithmetic = [
            {"tool_receipt_id": key, **_safe(value)}
            for key, value in state.get("tool_attempts", {}).items()
            if isinstance(value, dict)
            and value.get("status") == "success"
            and value.get("scope") == "deterministic_only_not_reviewed"
            and isinstance(value.get("data"), dict)
            and "value" in value["data"]
        ]
        reviewed_calculations = _safe(
            (state.get("last_reviewed") or {}).get("calculations", {})
        )
        if arithmetic or reviewed_calculations:
            body += (
                '<details class="card"><summary>已算出的结果（算术回执，会计解释未审结）</summary>'
                + _render(arithmetic, links)
            )
            if reviewed_calculations:
                body += "<h3>最近已保存审查轮的计算记录</h3>" + _render(
                    reviewed_calculations, links
                )
            body += "</details>"
        body += "<h2>完整研究／审查时间线</h2>" + "".join(
            _event_html(e, links, issues) for e in public_records
        )
        if not public_records:
            body += "<p>没有匹配到该会话的可展示研究输出；这不代表公司不合格。</p>"
        export = {
            "schema_version": "research-notebook/1",
            "status": "unreviewed_archive",
            "summary": summary,
            "events": public_records,
            "arithmetic_receipts": arithmetic,
            "last_reviewed_calculations": reviewed_calculations,
            "cost_events": [
                {k: v for k, v in e.items() if k != "content"} for e in records
            ],
            "sources": links,
        }
        _atomic(Path(summary["path"]), _document(summary["name"] + "研究笔记", body))
        _atomic(
            Path(summary["json_path"]), json.dumps(export, ensure_ascii=False, indent=2)
        )
        result["sessions"].append(summary)
        cards.append(
            f'<div class="card"><h2><a href="{filename}.html">{_esc(summary["name"])}</a></h2><p>{summary["event_count"]} 次研究／审查输出 · {summary["fact_count"]} 条数据摘录 · {summary["conclusion_count"]} 项判断版本 · {summary["review_issue_count"]} 条审查意见</p><p>输入 {summary["usage"]["input_tokens"]:,} / 输出 {summary["usage"]["output_tokens"]:,} token（含压缩）</p>'
            + research_history
            + numeric_preview
            + "</div>"
        )
    result["status"] = "rendered"
    editorial_link = (
        '<p class="card"><a href="management_review.html">经营变化与管理层解释：本次从缓存原文补读的具体例子</a>（本次离线整理，未经过独立模型审查）</p>'
        if (output / "management_review.html").is_file()
        else ""
    )
    _atomic(
        output / "index.html",
        _document(
            "已有研究输出",
            style + "<h1>已有研究输出</h1>" + intro + editorial_link + "".join(cards),
        ),
    )
    _atomic(output / "index.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result
