"""Email-safe HTML rendered from current verified research state, without prose LLMs."""

from __future__ import annotations

import html
import math
from collections import Counter


def esc(value):
    return html.escape(str(value if value is not None else "—"), quote=True)


def table(rows):
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(
            '<tr><td style="padding:10px 0;border-bottom:1px solid #e7edf1;color:#526477">'
            + esc(label)
            + '</td><td align="right" style="padding:10px 0;border-bottom:1px solid #e7edf1;font-weight:600">'
            + esc(value)
            + "</td></tr>"
            for label, value in rows
        )
        + "</table>"
    )


def section(title, content):
    return (
        '<tr><td class="section" style="padding:22px 28px;border-bottom:1px solid #e7edf1"><h2 style="font-size:17px;margin:0 0 12px;color:#122e43">'
        + esc(title)
        + "</h2>"
        + content
        + "</td></tr>"
    )


def percent(metric):
    value = (metric or {}).get("value")
    return (
        f"{value:.1%}" if type(value) in {int, float} and math.isfinite(value) else "—"
    )


def render_email(title: str, message: str, report: dict | None = None) -> str:
    context = report or {}
    snapshot = context.get("snapshot")
    kind = context.get("kind", "status")
    payload = context.get("payload", {})
    if kind == "research_report_ready":
        from .contracts import fingerprint
        from .research_contracts import report_acceptance
        from .research_release import render_research_report

        reviewed = payload.get("report") or {}
        errors = report_acceptance(reviewed)
        issuer = (reviewed.get("company") or {}).get("id")
        expected = fingerprint({"company": issuer, "payload": reviewed})
        if (
            not errors
            and issuer == payload.get("company_id")
            and payload.get("staging_version") == expected
            and reviewed.get("status") not in {"revoked", "superseded"}
        ):
            return render_research_report(reviewed)
        # Never fall back to stale report text if its current gate fails.
        kind = "research_blocked"
        payload = {
            "company_id": payload.get("company_id"),
            "reason_codes": ["research_report_notification_validation_failed"],
            "next_action": "检查报告版本、证据与审查记录；本通知不展示旧结论。",
        }
        snapshot = None
        message = "研究报告通知未通过发送前检查，已阻止展示。"
    labels = {
        "weekly": "每周研究进展",
        "correction": "研究结论更正",
        "breaker": "失效条件变化",
        "status": "研究进度更新",
        "acceptance": "研究链路验收",
        "acceptance_blocker": "研究受阻：需要处理",
        "research_blocked": "研究受阻：未形成有效报告",
        "research_preflight": "研究开始前检查",
        "research_report_correction": "已发送研究报告更正",
    }
    headline = labels.get(kind, "研究进度更新")
    body = ""
    if isinstance(snapshot, dict):
        campaign = snapshot.get("acceptance_campaign") or {}
        if campaign.get("enabled"):
            body += section(
                "一次性研究链路验收",
                table(
                    [
                        ("验收编号", campaign.get("id")),
                        ("状态", campaign.get("status")),
                        ("停止原因", campaign.get("reason")),
                        (
                            "本轮模型调用",
                            campaign.get("usage", {}).get("model_calls", 0),
                        ),
                        (
                            "实际输入 token",
                            campaign.get("usage", {}).get("input_tokens", 0),
                        ),
                        (
                            "实际输出 token",
                            campaign.get("usage", {}).get("output_tokens", 0),
                        ),
                        ("结束后暂停模型花费", campaign.get("hold_model_spending")),
                    ]
                )
                + "<p>这是少数公司端到端验收，不代表全池研究完成；未形成报告不等于公司财务不合格。</p>",
            )
            for issuer, result in campaign.get("outcomes", {}).items():
                body += section(
                    f"验收公司 {issuer}",
                    table(
                        [
                            (label, result.get(key, "—"))
                            for key, label in (
                                ("status", "验收结果"),
                                ("stage", "当前阶段"),
                                ("screen", "初筛状态"),
                                ("before_facts", "本步开始事实数"),
                                ("after_facts", "本步结束事实数"),
                            )
                        ]
                    )
                    + "<p>阻塞项："
                    + esc(
                        "；".join(
                            str(e.get("code", "")) + ":" + str(e.get("field", ""))
                            for e in result.get("errors", [])[:8]
                        )
                        or result.get("reason", "无")
                    )
                    + "</p>",
                )
        companies = snapshot.get("companies", [])
        active = [c for c in companies if c.get("stage") != "excluded"]
        published = [
            c
            for c in active
            if (c.get("publication") or {}).get("status") == "published"
        ]
        from .reporting import _candidate

        candidates = [
            c
            for c in published
            if (
                _candidate(c)
                if c["publication"].get("policy_version") == "model-led-research/1"
                else c["publication"].get("candidate") is True
            )
        ]
        # A queued digest may describe only the exact versions bound to its outbox.
        if kind == "weekly":
            refs = {
                r["company_id"]: r["version"]
                for r in payload.get("publication_refs", [])
            }
            candidates = [
                c
                for c in candidates
                if refs.get(c["id"]) == c["publication"].get("version")
            ]
        lead = (
            "暂无正式候选，正在补齐证据并核验。"
            if not candidates
            else f"本期有 {len(candidates)} 家已核验研究候选。"
        )
        if not candidates and campaign.get("hold_model_spending") is True:
            lead = "暂无正式候选。本轮已停止模型支出，仍有未解决的阻塞；不是验证通过。"
        lead += " 缺数据或暂未入选，不等于公司财务不合格。"
        cards = [
            ("在池或待确认", len(active)),
            ("已核验报告", len(published)),
            ("正式候选", len(candidates)),
        ]
        body += section(
            "当前状态",
            '<p style="margin:0 0 18px">'
            + esc(lead)
            + '</p><table role="presentation" width="100%"><tr>'
            + "".join(
                '<td width="33%" style="background:#eff5f7;padding:14px 8px;text-align:center;border-radius:6px"><div style="font-size:27px;font-weight:700;color:#163c54">'
                + f"{v:,}"
                + '</div><div style="font-size:12px;color:#536b7c">'
                + esc(k)
                + "</div></td>"
                for k, v in cards
            )
            + "</tr></table>",
        )
        stages = Counter(c.get("stage", "unknown") for c in active)
        body += section(
            "研究进度",
            table(
                [
                    ("尚未处理", stages.get("discovered", 0)),
                    (
                        "补数／证据准备",
                        sum(stages.get(k, 0) for k in ("data", "evidence")),
                    ),
                    (
                        "缺数据或核验受阻",
                        sum(stages.get(k, 0) for k in ("blocked", "evidence_blocked")),
                    ),
                    (
                        "初筛未通过",
                        sum(
                            c.get("screen") == "fail" and c.get("stage") == "screened"
                            for c in active
                        ),
                    ),
                    (
                        "分析／待核验",
                        sum(
                            stages.get(k, 0)
                            for k in ("analysis", "review", "verification")
                        ),
                    ),
                    ("历史退池记录", len(companies) - len(active)),
                ]
            )
            + '<p style="font-size:12px;color:#687b89;margin:12px 0 0">统计覆盖全部登记公司；有限批次完成不代表全池研究完成。</p>',
        )
        candidate_content = '<p style="margin:0;color:#687b89">当前没有满足全部适用发布条件的候选。后续仅展示通过独立核验的结论。</p>'
        if candidates:
            candidate_content = ""
            for company in candidates:
                pub = company["publication"]
                metrics = pub.get("metrics", {})
                candidate_content += (
                    '<div style="padding:14px 0;border-bottom:1px solid #e7edf1"><strong>'
                    + esc(company.get("name", ""))
                    + " · "
                    + esc(company.get("code", company["id"]))
                    + "</strong>"
                    + table(
                        [
                            ("正常化 ROIC", percent(metrics.get("normalized_roic"))),
                            ("正常化 ROE", percent(metrics.get("normalized_roe"))),
                            ("PB 隐含回报率", percent(metrics.get("implied_ke_pb"))),
                            ("PE 隐含回报率", percent(metrics.get("implied_ke_pe"))),
                            (
                                "正常化股东 FCF 收益率",
                                percent(metrics.get("fcf_yield")),
                            ),
                            ("行情日期", pub.get("quote_date", "—")),
                        ]
                    )
                )
                for conclusion in pub.get("conclusions", []):
                    if conclusion.get("key") in {"peer_thesis", "thesis_breaker"}:
                        candidate_content += (
                            '<p style="margin:10px 0">'
                            + esc(conclusion.get("reason", ""))
                            + "</p>"
                        )
                candidate_content += "</div>"
        body += section("正式研究候选", candidate_content)
        issues = Counter()
        for company in active:
            buckets = set()
            for error in company.get("errors", []):
                code = error.get("code", "")
                bucket = "其他证据或核验缺口"
                for needle, label in [
                    ("conflict", "财务数据或口径冲突"),
                    ("original", "财报原文尚未齐备或核验未完成"),
                    ("history", "历史数据覆盖不足"),
                    ("quote", "行情缺失或过期"),
                    ("classification", "行业模板尚待确认"),
                    ("budget", "本日额度已用完，等待后续批次"),
                ]:
                    if needle in code:
                        bucket = label
                        break
                buckets.add(bucket)
            issues.update(buckets)
        if issues:
            body += section(
                "主要待解决事项",
                table(
                    [(name, f"{count:,} 家") for name, count in issues.most_common(6)]
                )
                + '<p style="font-size:12px;color:#687b89">同一公司可有多类缺口，以上数字不相加。</p>',
            )
        coverage = snapshot.get("coverage", {})
        body += section(
            "研究池覆盖",
            table(
                [
                    (name, "已确认" if coverage.get(key) is True else "尚未确认")
                    for key, name in [
                        ("a500", "A500 名录"),
                        ("shanghai", "沪市名录"),
                        ("shenzhen", "深市名录"),
                        ("beijing", "北市名录"),
                        ("reit", "境内 REIT 全量名录"),
                    ]
                ]
                + [("待补市值", coverage.get("size_pending", "—"))]
            ),
        )
        budgets = snapshot.get("budgets", {})
        limits = snapshot.get("limits") or context.get("limits", {})
        body += section(
            "今日资源使用",
            table(
                [
                    (label, f"{budgets.get(key, 0)} / {limits.get(key, default)}")
                    for key, label, default in [
                        ("acquisition", "补数发行人", 50),
                        ("research", "深研公司", 20),
                        ("model_calls", "模型调用（含核验及失败）", 80),
                        ("input_token_reservations", "输入 token 预留", "未提供"),
                        ("output_token_reservations", "输出 token 预留", "未提供"),
                    ]
                ]
            )
            + "<p>预留额度与实际消耗分别记录；提高 token 上限不会重置已用额度，也不会增加模型调用次数。</p>",
        )
        policy = snapshot.get("acquisition_policy", {})
        if policy.get("phase") == "bootstrap":
            body += section(
                "首轮建库进度",
                table(
                    [
                        ("首轮池", policy.get("cohort_size", "—")),
                        (
                            "已结束首次采集或明确排除",
                            policy.get("first_pass_finished_or_excluded", "—"),
                        ),
                        ("尚待首次采集", policy.get("first_pass_pending", "—")),
                    ]
                )
                + "<p>首次采集结束不代表资料齐备或通过核验；首轮遍历后自动恢复日常补数额度。</p>",
            )
        stamp = snapshot.get("as_of", "—")
    else:
        stamp = context.get("as_of", "—")
        body += section(
            "通知内容",
            "".join(
                '<p style="margin:0 0 12px;overflow-wrap:anywhere">'
                + esc(line)
                + "</p>"
                for line in message.splitlines()
                if line.strip()
            ),
        )
    if kind in {"acceptance_blocker", "research_blocked", "research_preflight"}:
        reasons = {
            "invalid_response": "模型返回内容未通过格式校验",
            "incomplete_response": "模型输出未完整结束",
            "screen_metric_missing": "初筛所需指标尚未取得",
            "original_document_parse_failed": "原始财报文件缺失或解析失败",
            "original_text_incomplete": "原文页面尚未完整核实",
            "acceptance_extraction_limit": "本公司已达到本轮提取批次上限",
            "acceptance_model_budget": "本轮模型预算已用尽",
            "primary_fact_evidence_required": "财报原文事实不足，不能进入完整分析",
            "research_report_notification_validation_failed": "报告版本或核验结果不满足发送条件",
            "acceptance_deadline": "原验收截止时间已过，不能恢复付费调用",
            "acceptance_budget": "原验收可用预算不足",
        }
        codes = payload.get("reason_codes") or ["未提供原因，需检查运行记录"]
        notice = table(
            [
                ("受影响公司", payload.get("company_id", "—")),
                ("验收批次", payload.get("campaign_id", "—")),
                ("阻塞原因", "；".join(reasons.get(code, code) for code in codes)),
                (
                    "下一步",
                    payload.get("next_action")
                    or "定位对应原文或错误记录，修复并验证后才重试；不绕过核验。",
                ),
                ("实际模型调用", payload.get("usage", {}).get("model_calls", "未提供")),
                (
                    "实际输入 token",
                    payload.get("usage", {}).get("input_tokens", "未提供"),
                ),
                (
                    "实际输出 token",
                    payload.get("usage", {}).get("output_tokens", "未提供"),
                ),
                (
                    "输入预留累计",
                    payload.get("usage", {}).get("input_token_reservations", "未提供"),
                ),
                (
                    "输出预留累计",
                    payload.get("usage", {}).get("output_token_reservations", "未提供"),
                ),
                ("用量未知的调用", payload.get("unknown_usage_calls", "未提供")),
            ]
        )
        body = (
            section(
                "本轮验收失败，付费运行已停止"
                if payload.get("scope") == "campaign"
                else "公司研究已受阻，未通过验收",
                notice
                + (
                    "<p>本轮未形成有效报告，未切换新链路。资料未处理或证据不足不代表公司投资不合格。保留原账本，不自动恢复付费运行。</p>"
                    if payload.get("scope") == "campaign"
                    else "<p>此通知在公司停止推进时发出，不等待整轮结束。其他公司可独立继续；受影响公司不能列为正式候选。</p>"
                ),
            )
            + body
        )
    if kind in {"correction", "breaker", "research_report_correction"}:
        notice = table(
            [
                ("公司", payload.get("company_id", "—")),
                (
                    "变化原因",
                    payload.get("reason", payload.get("state", "待查看核验记录")),
                ),
            ]
        )
        body = (
            section(
                headline,
                '<div style="border-left:4px solid #ba4a35;padding-left:12px">'
                + notice
                + "<p>受影响的旧结论不再作为有效候选依据，请以当前核验状态为准。</p></div>",
            )
            + body
        )
    note = context.get("note")
    if note:
        body += section("运行安排", '<p style="margin:0">' + esc(note) + "</p>")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + esc(title)
        + '</title><style>@media(max-width:520px){.section{padding:18px 16px!important}.outer{padding:12px 0!important}}</style></head><body style="margin:0;background:#eef2f5;color:#233c4d;font-family:Arial,\'Microsoft YaHei\',sans-serif;font-size:15px;line-height:1.65"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td class="outer" align="center" style="padding:28px 12px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;background:#fff;border:1px solid #dce5ec;border-radius:10px;overflow:hidden;table-layout:fixed;word-wrap:break-word"><tr><td style="background:#15364b;color:#fff;padding:26px 28px"><div style="font-size:11px;letter-spacing:2px;color:#b4d3df">FUNDAMENTAL QUANT</div><h1 style="font-size:25px;line-height:1.35;margin:8px 0">'
        + esc(headline)
        + '</h1><div style="color:#c5d8e2;font-size:13px">数据截至 '
        + esc(stamp)
        + " · 北京时间</div></td></tr>"
        + body
        + '<tr><td style="padding:20px 28px;color:#748491;font-size:12px">本邮件由研究库的当前状态生成；财务数值和达标状态来自代码计算，只有有效发布版本才展示候选结论。<br>'
        + esc(title)
        + "</td></tr></table></td></tr></table></body></html>"
    )
