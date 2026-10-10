"""Deterministic reports built only from the service's current reviewed snapshot."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by with inside try
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=".fundamental-",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def visible_company(company: dict) -> dict:
    """Prevent an obsolete publication's claims leaking into any current surface."""
    result = dict(company)
    staged = result.get("reviewed_report")
    if staged:
        from .session_review import accept_publication

        if company.get("stage") == "excluded" or accept_publication(staged):
            result["reviewed_report"] = None
    publication = company.get("publication")
    if publication and (
        company.get("stage") == "excluded"
        or company.get("listing_status") in {"inactive", "out_of_universe"}
    ):
        publication = {
            "version": publication.get("version"),
            "status": "revoked",
            "reason": company.get(
                "exclusion_reason", "Issuer excluded from current universe"
            ),
        }
        result["publication"] = publication
    if (
        publication
        and publication.get("status") == "published"
        and publication.get("policy_version") in {"model-led-research/1", "raw-pdf-sessions/1"}
    ):
        from .research_contracts import report_acceptance

        errors = report_acceptance(publication)
        if errors:
            publication = {
                "version": publication.get("version"),
                "status": "blocked",
                "reason": "当前模型主导报告未满足有效报告合同",
                "issues": errors,
            }
            result["publication"] = publication
    if publication and publication.get("status") != "published":
        result["publication"] = {
            key: publication[key]
            for key in ("version", "status", "as_of", "reason", "issues", "replaced_by")
            if key in publication
        }
    return result


def _document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Cache-Control" content="no-store">'
        f"<title>{_escape(title)}</title>"
        "<style>body{font-family:system-ui;margin:2rem;line-height:1.5}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;"
        "padding:.5rem;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "caption{text-align:left;font-weight:bold}</style>"
        f"<body><h1>{_escape(title)}</h1>{body}</body></html>"
    )


def _section(label: str, value: object) -> str:
    """Tables preserve the exact data; prose is never invented by the renderer."""
    heading = f"<h2>{_escape(label)}</h2>"
    if not value:
        return heading + "<p>未提供 / 不适用；不视为已完成。</p>"
    if isinstance(value, dict):
        rows = value.items()
    elif isinstance(value, list):
        rows = enumerate(value, start=1)
    else:
        return heading + f"<pre>{_escape(value)}</pre>"
    return (
        heading
        + "<table><tbody>"
        + "".join(
            f"<tr><th>{_escape(key)}</th><td><pre>"
            f"{_escape(_json(item) if isinstance(item, (list, dict)) else item)}"
            "</pre></td></tr>"
            for key, item in rows
        )
        + "</tbody></table>"
    )


def _number(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _metric(metrics: dict, name: str) -> float | None:
    item = metrics.get(name)
    if not isinstance(item, dict) or item.get("status") != "derived":
        return None
    return _number(item.get("value"))


def _percent(value: float | None) -> str:
    return "未提供有效数值" if value is None else f"{value * 100:.2f}%"


def _gate(value: object) -> str:
    return "达标" if value is True else "未达标" if value is False else "无法判断"


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _known(value: object) -> str:
    if value is None or value == "":
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _in_universe(company: dict) -> bool:
    return company.get("stage") != "excluded" and company.get("listing_status") not in {
        "inactive",
        "out_of_universe",
    }


def _candidate(company: dict) -> bool:
    publication = company.get("publication") or {}
    if publication.get("policy_version") in {"model-led-research/1", "raw-pdf-sessions/1"}:
        from .research_contracts import report_acceptance

        return (
            _in_universe(company)
            and publication.get("status") == "published"
            and publication.get("candidate") is True
            and (publication.get("decision") or {}).get("action") == "candidate"
            and not report_acceptance(publication)
        )
    return (
        _in_universe(company)
        and publication.get("status") == "published"
        and publication.get("candidate") is True
        and (publication.get("metrics") or {}).get("hurdle_pass", {}).get("value")
        is True
        and (publication.get("qualification") or {}).get("candidate", True) is True
    )


def stage_progress(companies: list[dict]) -> dict:
    """Count only recorded stages/evidence; missing receipts never imply zero."""
    companies = [visible_company(company) for company in companies]
    targets = [c for c in companies if _in_universe(c) and not c.get("peer_only")]
    screens = dict.fromkeys(
        ("pass", "fail", "unknown", "pending", "cycle_exception"), 0
    )
    for company in targets:
        status = company.get("screen")
        if status in {"pass", "fail", "cycle_exception"}:
            screens[status] += 1
        elif (
            company.get("stage", "pending") in {"discovered", "pending", "unprocessed"}
            and not company.get("evidence_progress")
            and not company.get("errors")
        ):
            screens["pending"] += 1
        else:
            screens["unknown"] += 1
    evidence = {}
    for field in (
        "original_page_count",
        "readable_original_page_count",
        "fact_count",
        "mechanically_checked_fact_count",
    ):
        values = [
            _count((c.get("evidence_progress") or {}).get(field)) for c in targets
        ]
        known = [v for v in values if v is not None]
        evidence[field] = {
            "value": sum(known) if known else None,
            "reported_issuers": len(known),
            "unreported_issuers": len(targets) - len(known),
        }
    return {
        "target_issuers": len(targets),
        "screens": screens,
        "evidence": evidence,
        "peer_dependencies": sum(
            _in_universe(c) and bool(c.get("peer_only")) for c in companies
        ),
        "formal_candidates": sum(_candidate(c) for c in targets),
    }


def _progress_overview(companies: list[dict], progress: dict) -> str:
    screens = progress["screens"]
    body = "<h2>阶段成果摘要</h2>"
    body += (
        f"<p>主研究池：{progress['target_issuers']} 家；"
        f"同行依赖：{progress['peer_dependencies']} 家（单列，不计入主池初筛分母）。</p>"
        "<table><thead><tr><th>初筛通过</th><th>初筛不通过</th><th>无法判断</th>"
        "<th>尚未初筛</th><th>周期例外待深研</th></tr></thead><tbody><tr>"
        + "".join(
            f"<td>{screens[key]}</td>"
            for key in ("pass", "fail", "unknown", "pending", "cycle_exception")
        )
        + "</tr></tbody></table>"
        "<p>初筛通过只表示进入证据准备；周期例外单列。无法判断与财务不合格分开统计，均不等于正式候选。</p>"
    )
    body += "<table><thead><tr><th>证据进度</th><th>已记录数量</th><th>回执覆盖</th></tr></thead><tbody>"
    for field, label in (
        ("original_page_count", "已取得原文页"),
        ("readable_original_page_count", "已提取可读原文文本页"),
        ("fact_count", "已记录事实项"),
        ("mechanically_checked_fact_count", "已通过机械检查的事实项"),
    ):
        item = progress["evidence"][field]
        body += (
            f"<tr><th>{label}</th><td>{_known(item['value'])}</td><td>"
            f"{item['reported_issuers']}/{progress['target_issuers']} 家已记录；"
            f"{item['unreported_issuers']} 家未知</td></tr>"
        )
    body += (
        "</tbody></table><p>按每家公司最近一次证据回执汇总；不是本轮新增量。"
        "原文文本提取不等于模型已完成阅读，机械检查不等于独立会计核验或投资结论；未知项不按零计。</p>"
    )
    if progress["formal_candidates"]:
        return body
    body += "<h2>阶段证据样例（尚无正式候选）</h2><p>以下仅展示已有回执中的原文、事实数量及缺口，不是估值结论，也不构成推荐。完整记录见各公司报告。</p>"
    samples = sorted(
        (
            c
            for c in companies
            if _in_universe(c) and not c.get("peer_only") and c.get("evidence_progress")
        ),
        key=lambda c: str(c.get("code") or c.get("id") or ""),
    )[:5]
    if not samples:
        return (
            body + "<p>尚无可展示的证据回执；已取得原文、已核对事实与缺口均未知。</p>"
        )
    body += "<p>按代码排序展示最多 5 家，仅用于查看进展，不是优先级排名。</p><table><thead><tr><th>公司</th><th>已记录证据</th><th>原文财期 / 回执时间</th><th>当前缺口</th></tr></thead><tbody>"
    for company in samples:
        receipt = company["evidence_progress"]
        identity = str(company.get("id") or company.get("code"))
        filename = hashlib.sha256(identity.encode()).hexdigest() + ".html"
        periods = receipt.get("original_periods")
        period_text = (
            "、".join(map(str, periods))
            if isinstance(periods, list) and periods
            else "未知"
        )
        errors = [e for e in company.get("errors", []) if isinstance(e, dict)]
        gaps = (
            "；".join(
                str(e.get("code", "未标明错误类型"))
                + (f"（{e['field']}）" if e.get("field") else "")
                for e in errors[:3]
            )
            or "缺口未记录；不能据此认定资料齐备"
        )
        if len(errors) > 3:
            gaps += f"；另有 {len(errors) - 3} 项，见公司报告"
        publication = company.get("publication") or {}
        if publication.get("status") in {"revoked", "stale", "superseded"}:
            gaps += f"；研究结论当前无效（{publication['status']}）"
        counts = (
            f"原文页 {_known(_count(receipt.get('original_page_count')))}；"
            f"可读文本页 {_known(_count(receipt.get('readable_original_page_count')))}；"
            f"机械核对事实 {_known(_count(receipt.get('mechanically_checked_fact_count')))}"
        )
        body += (
            f'<tr><td><a href="companies/{filename}">{_escape(company.get("code", identity))} '
            f"{_escape(company.get('name', ''))}</a></td><td>{_escape(counts)}</td>"
            f"<td>{_escape(period_text)}<br>{_escape(_known(receipt.get('updated_at')))}</td><td>{_escape(gaps)}</td></tr>"
        )
    return body + "</tbody></table>"


def _execution_overview(snapshot: dict) -> str:
    baseline = snapshot.get("baseline") or {}
    runtime = snapshot.get("runtime") or {}
    body = "<h2>本轮采集基线</h2><table><tbody>"
    for key, label in (
        ("size", "冻结基线公司数"),
        ("first_pass_finished", "已完成首轮采集尝试"),
        ("first_pass_pending", "待首轮采集"),
    ):
        body += f"<tr><th>{label}</th><td>{_known(_count(baseline.get(key)))}</td></tr>"
    body += "</tbody></table><p>首轮采集尝试完成不代表资料齐备、通过核验或成为候选；基线之外的公司仍保留在全池清单。</p>"
    body += "<h2>运行证据</h2><table><tbody>"
    labels = (
        ("state", "状态记录"),
        ("host_alive", "调度宿主进程存活"),
        ("child_alive", "批次子进程存活"),
        ("last_heartbeat", "最近心跳"),
        ("job_state", "任务状态记录"),
        ("phase", "当前阶段"),
        ("company_id", "当前公司"),
        ("updated_at", "进展更新时间"),
        ("error_type", "错误类型"),
    )
    for key, label in labels:
        value = runtime.get(key)
        if key in {"host_alive", "child_alive"} and type(value) is not bool:
            value = None
        body += f"<tr><th>{label}</th><td>{_escape(_known(value))}</td></tr>"
    return (
        body
        + "</tbody></table><p>历史 running 记录不能证明当前仍在运行；应结合进程存活与心跳时间判断，缺失证据显示未知。</p>"
    )


def summary_lines(company: dict) -> list[str]:
    """Describe reviewed calculations without model inference or target prices."""
    publication = visible_company(company).get("publication") or {}
    if publication.get("status") != "published":
        return [
            f"无当前有效研究结论；发布状态：{publication.get('status', 'unpublished')}。"
            + (f"原因：{publication['reason']}。" if publication.get("reason") else "")
        ]
    if publication.get("policy_version") in {"model-led-research/1", "raw-pdf-sessions/1"}:
        return _adaptive_summary(publication)
    metrics = publication.get("metrics") or {}
    gates = metrics.get("hurdle_pass") or {}
    hurdle = _number(gates.get("hurdle"))
    ke_pb = _metric(metrics, "implied_ke_pb")
    ke_pe = _metric(metrics, "implied_ke_pe")
    cash = _metric(metrics, "fcf_yield")
    margin = _metric(metrics, "safety_margin")
    template = company.get("template")
    lines = [
        (
            f"研究日期：{publication.get('as_of', '未记录')}；"
            f"价格日期：{publication.get('quote_date', '未记录')}；"
            f"回报门槛：{_percent(hurdle)}。"
        )
    ]
    if template == "reit":
        affo = _metric(metrics, "affo_yield")
        gate = affo >= hurdle if affo is not None and hurdle is not None else None
        lines.append(f"AFFO 门槛：{_gate(gate)}；正常化 AFFO yield：{_percent(affo)}。")
        lines.append(
            "普通企业 PE/PB-implied Ke 与 FCF 门槛不适用；Ke 安全余量不适用。"
            "AFFO、DPU 与可供分配金额分别核验，有限经营权不使用永续终值。"
        )
    else:
        value_gate = (
            gates.get("value_gate")
            if ke_pb is not None and ke_pe is not None and hurdle is not None
            else None
        )
        lines.append(
            f"估值门槛：{_gate(value_gate)}；PB-implied Ke：{_percent(ke_pb)}；"
            f"PE-implied Ke：{_percent(ke_pe)}。"
        )
        if template in {"bank", "insurance"}:
            lines.append(
                "现金流门槛：不适用；银行／保险不套用普通企业 FCF yield 门槛，行业经营和资本质量另行核验。"
            )
        else:
            cash_gate = (
                gates.get("cash_gate")
                if cash is not None and hurdle is not None
                else None
            )
            lines.append(
                f"现金流门槛：{_gate(cash_gate)}；正常化股东 FCF yield：{_percent(cash)}。"
            )
        margin_text = (
            "未提供有效数值" if margin is None else f"{margin * 100:+.2f} 个百分点"
        )
        lines.append(
            f"Ke 安全余量（PB-implied Ke 减回报门槛）：{margin_text}；不是价格折价率。"
        )
    if publication.get("candidate") is True and gates.get("value") is True:
        lines.append("资格：正式研究候选；投资组合是否纳入仍需人工判断。")
    elif publication.get("candidate") is True:
        lines.append("资格：候选标记与计算门槛记录不一致，无法确认正式候选资格。")
    else:
        lines.append("资格：未进入正式研究候选；单项达标不代表全部条件通过。")
    qualification = publication.get("qualification", {})
    if qualification:
        labels = {
            "all_scenarios_pass": "保守、基准和乐观情景均通过",
            "base_only": "基准情景通过、保守情景未通过；不满足正式候选条件",
            "optimistic_only": "仅乐观情景支持；不满足正式候选条件",
            "incomplete": "情景证据不足",
            "not_passed": "情景条件未全部通过",
        }
        lines.append(
            "情景资格："
            + labels.get(qualification.get("scenario_state"), "无法判断")
            + "。"
        )
    return lines


def _adaptive_summary(publication: dict) -> list[str]:
    from .research_contracts import report_acceptance

    if report_acceptance(publication):
        return ["当前模型主导报告未满足有效报告合同；不展示投资结论。"]
    decision = publication.get("decision", {})
    labels = {
        "candidate": "研究候选",
        "propose_candidate": "研究候选",
        "observe": "观察",
        "defer": "暂缓",
        "abandon": "放弃研究",
        "research": "继续研究",
        "continue_research": "继续研究",
        "narrow_scope": "缩小研究范围",
    }
    action = decision.get("action", "unknown")
    lines = [
        (
            f"研究日期：{publication.get('as_of', '未记录')}；"
            f"研究决定：{labels.get(action, action)}。"
        ),
        "财务门槛作为参考条件；候选资格以证据和独立审查为依据，不等于买入建议。",
    ]
    for key, label in (
        ("normalized_earnings", "正常化盈利"),
        ("normalized_shareholder_fcf", "正常化股东现金流"),
        ("normalized_fcf", "正常化现金流"),
        ("fcf_yield", "股东现金流收益率"),
        ("implied_ke_pb", "PB 隐含回报率"),
        ("implied_ke_pe", "PE 隐含回报率"),
    ):
        metric = (publication.get("metrics") or {}).get(key)
        if not isinstance(metric, dict):
            continue
        interval = metric.get("range") or {}
        values = [interval.get(k) for k in ("low", "base", "high")]
        ratio = "yield" in key or "ke_" in key
        formatter = (
            _percent
            if ratio
            else lambda value: f"{value:,.2f}" if _number(value) is not None else "未知"
        )
        if all(_number(value) is not None for value in values):
            lines.append(
                f"{label}：{formatter(values[0])}–{formatter(values[2])}；"
                f"基准 {formatter(values[1])}；"
                f"估算依赖：{'、'.join(metric.get('estimate_refs', [])) or '无'}。"
            )
        elif _number(metric.get("value")) is not None:
            lines.append(f"{label}：{formatter(metric['value'])}。")
    exceptions = decision.get("policy_exceptions") or decision.get("exceptions")
    if exceptions:
        lines.append("参考条件例外：" + _json(exceptions))
    lines.append(
        f"估算记录：{len(publication.get('estimates', []))} 项；"
        "请同时阅读依据、限制、敏感性及失效条件。"
    )
    return lines


def render_reports(root: Path, snapshot: dict) -> dict:
    """Replace current universe, company pages and machine exports atomically.

    Archived publication revisions remain in the service store, never in the
    current report directory. Each output file is independently atomic; the JSON
    includes its as_of value so consumers can detect mixed-generation reads.
    """
    root = Path(root)
    # Research drafts are inspectable audit material even when no publication
    # passes. Keep them outside the candidate/valuation snapshot consumed by Bots.
    from .session_notebook import render_session_notebooks

    notebooks = render_session_notebooks(root)
    notes_by_company = {}
    for note in notebooks.get("sessions", []):
        notes_by_company.setdefault(note["company_id"], note)
    companies = [visible_company(item) for item in snapshot.get("companies", [])]
    progress = stage_progress(companies)
    clean = dict(snapshot, companies=companies, stage_progress=progress)
    machine_json = _json(clean)  # Validate before replacing any current artifact.
    rows = []
    csv_rows = []
    current_pages = set()
    for company in companies:
        identity = str(company.get("id") or company.get("code"))
        filename = hashlib.sha256(identity.encode()).hexdigest() + ".html"
        current_pages.add(filename)
        title = f"{company.get('code', identity)} {company.get('name', '')}"
        publication = company.get("publication") or {}
        body = f"<p>快照：{_escape(snapshot.get('as_of'))}</p>"
        body += "<p>所有结论以当前发布状态为准；缺失或撤回不代表通过。</p>"
        note = notes_by_company.get(identity)
        if note:
            body += (
                '<p><a href="../research_notes/' + _escape(note["id"])
                + '.html">查看已经产生的数字、研究草稿及审查分歧</a> '
                '（原始研究记录，不等于核验通过）</p>'
            )
        body += "<h2>研究摘要</h2>" + "".join(
            f"<p>{_escape(line)}</p>" for line in summary_lines(company)
        )
        body += _section(
            "阶段与发布状态",
            {
                "template": company.get("template"),
                "stage": company.get("stage"),
                "status": company.get("status"),
                "screen": company.get("screen"),
                "publication_status": publication.get("status", "unpublished"),
                "version": publication.get("version"),
                "listing_status": company.get("listing_status"),
                "exclusion_reason": company.get("exclusion_reason"),
                "exclusion_sources": company.get("source_refs")
                if company.get("stage") == "excluded"
                else None,
            },
        )
        body += _section(
            "原文与事实积累进度（不代表通过核验）",
            company.get("evidence_progress", {}),
        )
        session = company.get("research_session") or {}
        if session:
            body += _section("原图持续研究：已审查的阶段结果", session)
            if session.get("report_path"):
                body += '<p><a href="../sessions/' + _escape(session["id"]) + '.html">查看逐轮研究与独立审查</a></p>'
        body += _section(
            "数据缺口及审查问题",
            company.get("errors", []) + publication.get("issues", []),
        )
        if publication.get("status") == "published":
            for label, key in (
                ("代码计算的估值与质量指标", "metrics"),
                ("六项核验结论", "conclusions"),
                ("同行比较", "peers"),
                ("同行指标与可比覆盖", "peer_comparison"),
                ("正常化调整桥接", "adjustments"),
                ("现金流分类及逐项独立核验", "cashflow_bridge"),
                ("三阶段情景计算", "scenarios"),
                ("代码计算的候选资格", "qualification"),
                ("Thesis breaker", "breakers"),
                ("独立上下文核验记录", "review"),
                ("原始证据定位", "source_refs"),
                ("逐期来源交叉核对", "source_crosschecks"),
                ("非财务封面图像核验及限制", "page_reviews"),
            ):
                body += _section(label, publication.get(key))
        body += "<details><summary>完整可审计记录</summary><pre>"
        body += _escape(_json(company)) + "</pre></details>"
        rendered = _document(title, body)
        if (
            publication.get("status") == "published"
            and publication.get("policy_version") in {"model-led-research/1", "raw-pdf-sessions/1"}
        ):
            from .research_release import render_research_report

            rendered = render_research_report(publication)
        elif company.get("reviewed_report"):
            from .research_release import render_research_report

            rendered = render_research_report(company["reviewed_report"])
        _atomic(root / "companies" / filename, rendered)
        values = {
            "code": company.get("code", identity),
            "name": company.get("name", ""),
            "template": company.get("template", "unknown"),
            "stage": company.get("stage", "pending"),
            "screen": company.get("screen", "unknown"),
            "status": company.get("status", "pending"),
            "publication_status": publication.get("status", "unpublished"),
            "errors": _json(company.get("errors", [])),
        }
        csv_rows.append(values)
        cells = "".join(f"<td>{_escape(value)}</td>" for value in values.values())
        rows.append(f'<tr><td><a href="companies/{filename}">查看</a></td>{cells}</tr>')
    # Removed instruments' old URLs must not continue serving a valid-looking report.
    for previous in (root / "companies").glob("*.html"):
        if previous.name not in current_pages:
            _atomic(
                previous, _document("报告不再有效", "<p>当前研究池未包含此发行人。</p>")
            )
    counts = Counter(str(item.get("status", "pending")) for item in companies)
    body = f"<p>快照：{_escape(snapshot.get('as_of'))}；当前已发现发行人：{len(companies)}</p>"
    if notebooks.get("sessions"):
        body += (
            '<h2>已经产生的研究内容</h2><p><a href="research_notes/index.html">'
            '打开研究记录：已有数字、估算、六项判断和审查意见</a></p>'
            '<p>未通过完整报告核验的内容仍可查看；争议、原文引用和历史版本一并保留。'
            '这些草稿不计入正式报告或候选数量。</p><ul>'
        )
        for note in notes_by_company.values():
            body += (
                '<li><a href="research_notes/' + _escape(note["id"]) + '.html">'
                + _escape(note.get("name") or note["company_id"]) + '</a>：'
                + str(note.get("fact_count", 0)) + ' 条事实记录，'
                + str(note.get("calculation_count", 0)) + ' 项计算提案，'
                + str(note.get("estimate_count", 0)) + ' 项估算（含历史版本）</li>'
            )
        body += '</ul>'
    excluded = sum(item.get("stage") == "excluded" for item in companies)
    body += f"<p>登记总数含历史退池记录；当前在池或待确认：{len(companies) - excluded}；已明确退池：{excluded}。退池记录仅供历史审计，不属于当前候选池。</p>"
    active = sum(
        (item.get("publication") or {}).get("status") == "published"
        for item in companies
    )
    candidates = progress["formal_candidates"]
    body += f"<p>当前有效发布：{active}；主研究池正式研究候选：{candidates}。名录覆盖与研究完成度分别记录，不能由已处理批次推断全市场完成。</p>"
    body += _progress_overview(companies, progress)
    body += _execution_overview(snapshot)
    campaign = snapshot.get("acceptance_campaign") or {}
    if campaign.get("enabled"):
        body += _section(
            "一次性端到端验收（不代表全池完成）",
            {
                "编号": campaign.get("id"),
                "状态": campaign.get("status"),
                "停止原因": campaign.get("reason"),
                "目标公司": campaign.get("targets", []),
                "本轮总额度": campaign.get("limits", {}),
                "本轮已用": campaign.get("usage", {}),
                "结束后暂停模型花费": campaign.get("hold_model_spending"),
            },
        )
        for issuer, outcome in campaign.get("outcomes", {}).items():
            body += _section(f"验收公司 {issuer}", outcome)
        body += "<p>这是有限目标公司的链路验收；接受事实、通过初筛、核验报告与正式候选分别统计。预算跨日、重启不刷新；没有报告不能视为公司不值得投资。</p>"
    budgets, limits = snapshot.get("budgets", {}), snapshot.get("limits", {})
    body += _section(
        "今日资源使用（已预留 / 上限）",
        {
            label: f"{budgets.get(key, '未提供')} / {limits.get(key, '未提供')}"
            for key, label in (
                ("model_calls", "模型调用，包含核验与失败"),
                ("input_token_reservations", "输入 token 预留"),
                ("output_token_reservations", "输出 token 预留"),
            )
        },
    )
    body += "<p>预留额度不是实际消耗；增加 token 上限不会重置已用额度，也不会增加调用次数。</p>"
    coverage = snapshot.get("coverage") or {}
    body += _section(
        "股票池发现覆盖",
        {
            label: "名录已覆盖；研究进度另列"
            if coverage.get(key) is True
            else "名录尚未覆盖"
            if coverage.get(key) is False
            else "覆盖未确认"
            for key, label in (
                ("a500", "A500"),
                ("shanghai", "上海"),
                ("shenzhen", "深圳"),
                ("beijing", "北京"),
                ("reit", "境内 REIT"),
            )
        },
    )
    for label, value in (
        ("完整覆盖回执", coverage),
        ("全池状态", counts),
        ("预算", snapshot.get("budgets", {})),
        ("当前资源上限", snapshot.get("limits", {})),
        ("首轮采集进度（不代表资料齐备）", snapshot.get("acquisition_policy", {})),
        ("持续核验指标", snapshot.get("metrics", {})),
        ("待独立抽查队列", snapshot.get("audit_queue", [])),
        ("数据链路错误", snapshot.get("errors", [])),
    ):
        body += f"<h2>{label}</h2><pre>{_escape(_json(value))}</pre>"
    body += "<table><caption>当前已发现研究池全量清单（含待处理与缺数据公司）</caption><thead><tr>"
    columns = [
        "查看",
        "代码",
        "名称",
        "模板",
        "阶段",
        "初筛",
        "状态",
        "发布状态",
        "错误",
    ]
    body += "".join(f"<th>{label}</th>" for label in columns)
    body += "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    _atomic(root / "index.html", _document("Fundamental Quant 研究总览", body))
    _atomic(root / "report.json", machine_json)
    output = io.StringIO(newline="")
    fields = [
        "code",
        "name",
        "template",
        "stage",
        "screen",
        "status",
        "publication_status",
        "errors",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    # Prevent spreadsheet formula execution when an issuer's name is imported.
    for row in csv_rows:
        writer.writerow(
            {
                key: (
                    "'" + str(value)
                    if str(value).startswith(("=", "+", "-", "@", "\t", "\r"))
                    else value
                )
                for key, value in row.items()
            }
        )
    _atomic(root / "report.csv", output.getvalue())
    return {
        "html": str(root / "index.html"),
        "json": str(root / "report.json"),
        "csv": str(root / "report.csv"),
    }


def bot_view(result: dict) -> dict:
    """Put current reviewed findings before bulky immutable audit snapshots."""
    def compact(company):
        item = visible_company(company)
        publication = item.get("publication") or {}
        report = publication if publication.get("status") == "published" else item.get("reviewed_report") or {}
        if report.get("policy_version") != "raw-pdf-sessions/1":
            return item
        return {
            "id": item.get("id"), "code": item.get("code"), "name": item.get("name"),
            "version": report.get("version"), "status": report.get("status"),
            "candidate": report.get("candidate") if report.get("status") == "published" else False,
            "decision": report.get("decision"), "as_of": report.get("as_of"),
            "quote_date": report.get("quote_date"), "conclusions": report.get("conclusions"),
            "metrics": report.get("metrics"), "coverage": report.get("coverage"),
            "usage": report.get("usage"), "source_refs": report.get("source_refs"),
            "full_report": (item.get("research_session") or {}).get("report_path"),
        }

    if "companies" in result:
        if result.get("longitudinal_reports"):
            # Reviewed group findings precede the large legacy registry snapshot.
            return {"longitudinal_reports": result["longitudinal_reports"],
                    **result, "companies": [compact(c) for c in result["companies"]]}
        return {**result, "companies": [compact(c) for c in result["companies"]]}
    return compact(result) if "publication" in result or "reviewed_report" in result else result


def format_query(result: dict) -> str:
    """Render a read-only Bot reply without a language-model rewrite."""
    if "companies" in result:
        result = dict(
            result, companies=[visible_company(x) for x in result["companies"]]
        )
    elif "publication" in result:
        result = visible_company(result)
    text = _json(bot_view(result))
    # Telegram's shared command transport sends one message. Keep a bounded,
    # explicitly partial view rather than silently losing oversized responses.
    if len(text.encode("utf-16-le")) // 2 > 2600:
        text = text.encode("utf-16-le")[:4800].decode("utf-16-le", errors="ignore")
        text += "\n… 展示已截断；不是完整研究结论。使用 --fundamental-quant-report 查看完整报告。"
    return "Fundamental Quant（当前核验记录）\n<pre>" + _escape(text) + "</pre>"
