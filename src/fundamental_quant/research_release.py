"""Reviewed research artifacts staged before the three-report rollout gate.

Staging never publishes an investment candidate or changes the active policy.
The database is authoritative; artifact hashes and the current evidence revision
are checked again whenever readiness is queried.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .contracts import CONCLUSION_KEYS, canonical, fingerprint
from .reporting import _atomic, _document, _escape, _json, _section
from .store import utcnow


def _acceptance(payload: dict) -> list[dict]:
    from .research_contracts import report_acceptance

    return report_acceptance(payload)


def _schema(store) -> None:
    with store.transaction() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS research_report_staging (
            version TEXT PRIMARY KEY, company_id TEXT NOT NULL,
            policy_version TEXT NOT NULL, input_hash TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL,
            json_path TEXT NOT NULL, html_path TEXT NOT NULL,
            json_hash TEXT NOT NULL, html_hash TEXT NOT NULL,
            created_at TEXT NOT NULL)"""
        )


def _bytes_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retire_artifacts(row, status: str, reason: str) -> None:
    """Old direct links show withdrawal; the immutable DB payload is the audit."""
    tombstone = {"version": row["version"], "status": status, "reason": reason}
    _atomic(Path(row["json_path"]), _json(tombstone))
    _atomic(
        Path(row["html_path"]),
        _document(
            "研究报告已撤回或被替代",
            "<p>本链接中的原结论不再有效；历史记录保留在研究审计库。</p>"
            + _section("版本状态", tombstone),
        ),
    )
    payload = json.loads(row["payload"])
    sid = payload.get("session_id", "")
    if len(sid) == 64 and all(c in "0123456789abcdef" for c in sid):
        _atomic(
            Path(row["html_path"]).parent.parent / "sessions" / (sid + ".html"),
            _document("研究阶段结果已失效", _section("版本状态", tombstone)),
        )


def _invalidate_notifications(db, row, reason: str) -> None:
    """Cancel pending old messages and correct only acknowledged channels."""
    notices = db.execute(
        "SELECT id,delivered,status FROM outbox WHERE kind='research_report_ready' "
        "AND json_extract(payload,'$.staging_version')=?",
        (row["version"],),
    ).fetchall()
    channels = set()
    for notice in notices:
        delivered = json.loads(notice["delivered"])
        channels.update(
            channel
            for channel, receipt in delivered.items()
            if receipt.get("status") in {"sent", "delivered"}
        )
        if notice["status"] == "pending":
            db.execute(
                "UPDATE outbox SET status='cancelled' WHERE id=?", (notice["id"],)
            )
    if not channels:
        return
    # A promoted version may have already queued the existing publication
    # correction. Do not send a second correction to those same channels.
    for correction in db.execute(
        "SELECT target_channels FROM outbox WHERE kind='correction' "
        "AND json_extract(payload,'$.version')=?",
        (row["version"],),
    ):
        channels.difference_update(json.loads(correction["target_channels"] or "[]"))
    if not channels:
        return
    oid = fingerprint({"kind": "research_report_correction", "version": row["version"]})
    previous = db.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
    completed = json.loads(previous["delivered"]) if previous else {}
    if previous:
        channels.update(json.loads(previous["target_channels"] or "[]"))
    pending = any(
        completed.get(channel, {}).get("status") not in {"sent", "delivered"}
        for channel in channels
    )
    payload = {
        "company_id": row["company_id"],
        "staging_version": row["version"],
        "reason": reason,
        "next_action": "原报告已撤回或被替代；以重新核验后的有效版本为准。",
    }
    db.execute(
        "INSERT INTO outbox(id,kind,payload,created_at,target_channels,status) "
        "VALUES(?,'research_report_correction',?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "target_channels=excluded.target_channels,status=excluded.status",
        (
            oid,
            canonical(payload),
            utcnow(),
            canonical(sorted(channels)),
            "pending" if pending else "delivered",
        ),
    )


def _research_section(title: str, value) -> str:
    labels = {
        "status": "状态",
        "reason": "依据",
        "rationale": "判断依据",
        "action": "研究决定",
        "fact_refs": "事实引用",
        "source_refs": "原文引用",
        "estimate_refs": "估算引用",
        "calculation_refs": "计算引用",
        "limitations": "限制",
        "counterevidence": "反证",
        "assumptions": "假设",
        "sensitivity": "敏感性",
        "invalidation": "失效条件",
        "value": "数值",
        "range": "情景区间",
        "low": "保守",
        "base": "基准",
        "high": "乐观",
        "formula": "计算方法",
        "unit": "单位",
        "currency": "币种",
        "period_end": "财期",
        "published_at": "披露日期",
        "page": "页码",
        "quote": "原文摘录",
        "checks": "逐项审查",
        "issues": "问题",
        "blind": "证据独立审查",
        "crosscheck": "结论交叉审查",
        "model_calls": "实际模型调用",
        "input_tokens": "实际输入 token",
        "output_tokens": "实际输出 token",
        "policy_exceptions": "参考条件例外",
    }

    def render(item):
        if isinstance(item, dict):
            return (
                "<table>"
                + "".join(
                    "<tr><th>"
                    + _escape(labels.get(key, key))
                    + "</th><td>"
                    + render(child)
                    + "</td></tr>"
                    for key, child in item.items()
                )
                + "</table>"
            )
        if isinstance(item, list):
            return (
                (
                    '<ul style="padding-left:20px">'
                    + "".join("<li>" + render(child) + "</li>" for child in item)
                    + "</ul>"
                )
                if item
                else "无记录"
            )
        return _escape(item if item is not None else "未提供")

    return (
        '<section style="margin:22px 0;padding:18px;background:#fff;'
        'border:1px solid #dce5ec;border-radius:8px">'
        '<h2 style="font-size:19px;color:#15364b;margin:0 0 12px">'
        + _escape(title)
        + "</h2>"
        + render(value)
        + "</section>"
    )


def render_research_report(payload: dict) -> str:
    """Render exact reviewed records; never ask a model to polish the report."""
    company = payload.get("company", {})
    title = f"{company.get('name') or company.get('id')} · 已核验研究报告"
    lifecycle = (
        "当前发布的已核验研究报告。"
        if payload.get("status") == "published"
        else "验收暂存报告；尚未因此启用新链路。"
    )
    body = (
        "<p>" + lifecycle + "研究结论不等于买入建议。原始事实与估算分开列示。</p>"
        f"<p>研究时点：{_escape(payload.get('as_of'))} · "
        f"政策版本：{_escape(payload.get('policy_version'))}</p>"
        + _research_section("研究决定与参考条件例外", payload.get("decision"))
    )
    labels = {
        "normalized_earnings": "正常化盈利",
        "capital_efficiency": "资本效率",
        "normalized_cashflow": "正常化股东现金流",
        "implied_return": "隐含回报及情景区间",
        "peer_thesis": "同行比较",
        "thesis_breaker": "投资依据与失效条件",
    }
    conclusions = payload.get("conclusions", [])
    if isinstance(conclusions, dict):
        conclusions = [dict(value, key=key) for key, value in conclusions.items()]
    by_key = {item.get("key"): item for item in conclusions}
    for key, label in labels.items():
        body += _research_section(label, by_key.get(key))
    source_map = {
        source["id"]: source
        for source in payload.get("evidence_packet", {}).get("sources", [])
        if source.get("id")
    }
    referenced_ids = {
        ref for claim in conclusions for ref in claim.get("source_refs", [])
    } | {
        fact["source_id"]
        for fact in payload.get("evidence_packet", {}).get("facts", [])
        if fact.get("source_id")
    }
    if payload.get("policy_version") == "raw-pdf-sessions/1":
        referenced_ids.update(payload.get("source_refs", []))
    sources = []
    source_refs = payload.get("source_refs") or sorted(
        {ref for claim in conclusions for ref in claim.get("source_refs", [])}
        | {
            fact["source_id"]
            for fact in payload.get("evidence_packet", {}).get("facts", [])
            if fact.get("source_id")
        }
    )
    for ref in source_refs:
        source = source_map.get(ref, {}) if isinstance(ref, str) else ref
        if source.get("id") not in referenced_ids:
            continue
        sources.append(
            {
                key: source[key]
                for key in (
                    "id",
                    "url",
                    "page",
                    "hash",
                    "document_sha256",
                    "document_path",
                    "origin_id",
                    "period_end",
                    "published_at",
                )
                if key in source
            }
        )
    calculated_metrics = {
        k: v
        for k, v in (payload.get("metrics") or {}).items()
        if not isinstance(v, dict) or not v.get("diagnostic_only")
    }
    diagnostics = {
        k: v
        for k, v in (payload.get("metrics") or {}).items()
        if isinstance(v, dict) and v.get("diagnostic_only")
    }
    for label, value in (
        ("代码计算结果与情景", calculated_metrics),
        ("口径诊断与模型提案（不作为估值输入）", diagnostics),
        ("估算（不是披露事实）", payload.get("estimates")),
        ("原始事实（保留披露值）", payload.get("evidence_packet", {}).get("facts")),
        ("原始事实引用", payload.get("fact_refs")),
        ("原文来源与定位", sources),
        ("逐项证据覆盖及限制", payload.get("coverage")),
        ("独立上下文审查及问题", payload.get("review")),
        ("实际调用与 token 成本", payload.get("usage")),
    ):
        body += _research_section(label, value)
    body += (
        "<p>独立上下文审查不能证明模型错误相互独立。"
        "新证据、数据更正或失效条件触发后，报告需要重新核验。</p>"
    )
    return _document(title, body)


def stage_report(store, payload: dict) -> dict:
    """Save one accepted report, without modifying active publications."""
    errors = _acceptance(payload)
    if errors:
        raise ValueError(f"research report failed acceptance: {canonical(errors)}")
    company_id = payload["company"]["id"]
    policy = payload["policy_version"]
    input_hash = payload["input_hash"]
    version = fingerprint({"company": company_id, "payload": payload})
    _schema(store)
    # Version-only names avoid trusting model-supplied company IDs as paths.
    root = store.root / "research_staging"
    json_path = root / f"{version}.json"
    html_path = root / f"{version}.html"
    with store.transaction() as db:
        current = db.execute(
            "SELECT input_hash,stage FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        if not current or current["input_hash"] != input_hash:
            raise ValueError("research report evidence is not the current packet")
        if current["stage"] == "excluded":
            raise ValueError("excluded issuer cannot stage a report")
        existing = db.execute(
            "SELECT status FROM research_report_staging WHERE version=?", (version,)
        ).fetchone()
        if existing and existing["status"] != "staged":
            raise ValueError("withdrawn report requires new evidence or review")
        _atomic(json_path, _json(payload))
        _atomic(html_path, render_research_report(payload))
        if not existing:
            previous = db.execute(
                "SELECT * FROM research_report_staging "
                "WHERE company_id=? AND status='staged'",
                (company_id,),
            ).fetchall()
            for prior in previous:
                promoted = db.execute(
                    "SELECT version FROM publications WHERE version=? AND status='published'",
                    (prior["version"],),
                ).fetchone()
                if promoted:
                    store._revoke(db, company_id, f"replaced by {version}")
                _retire_artifacts(prior, "superseded", f"replaced by {version}")
                _invalidate_notifications(db, prior, f"replaced by {version}")
            db.execute(
                "UPDATE research_report_staging SET status='superseded' "
                "WHERE company_id=? AND status='staged'",
                (company_id,),
            )
            db.execute(
                "INSERT INTO research_report_staging VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version,
                    company_id,
                    policy,
                    input_hash,
                    "staged",
                    canonical(payload),
                    str(json_path.resolve()),
                    str(html_path.resolve()),
                    _bytes_hash(json_path),
                    _bytes_hash(html_path),
                    utcnow(),
                ),
            )
            db.execute(
                "INSERT INTO events(company_id,kind,payload,created_at) "
                "VALUES(?,'research_report_staged',?,?)",
                (company_id, canonical({"version": version}), utcnow()),
            )
    return {
        "version": version,
        "company_id": company_id,
        "status": "staged",
        "json_path": str(json_path.resolve()),
        "html_path": str(html_path.resolve()),
    }


def release_status(store, policy_version: str | None = None) -> dict:
    """Recheck every staged report; a stale/tampered report never counts."""
    _schema(store)
    with store.transaction() as db:
        rows = db.execute(
            "SELECT r.*,c.input_hash AS current_hash,c.stage AS company_stage "
            "FROM research_report_staging r LEFT JOIN companies c "
            "ON c.id=r.company_id ORDER BY r.created_at,r.version"
        ).fetchall()
    reports, rejected = [], []
    for row in rows:
        item = dict(row)
        if item["status"] != "staged":
            continue
        if policy_version is not None and item["policy_version"] != policy_version:
            continue
        payload = json.loads(item["payload"])
        errors = _acceptance(payload)
        if item["input_hash"] != item["current_hash"]:
            errors.append({"code": "staged_evidence_changed"})
        if item["company_stage"] == "excluded":
            errors.append({"code": "staged_issuer_excluded"})
        for extension in ("json", "html"):
            path = Path(item[f"{extension}_path"])
            try:
                intact = _bytes_hash(path) == item[f"{extension}_hash"]
            except OSError:
                intact = False
            if not intact:
                errors.append({"code": f"staged_{extension}_artifact_changed"})
        result = {
            "version": item["version"],
            "company_id": item["company_id"],
            "policy_version": item["policy_version"],
            "json_path": item["json_path"],
            "html_path": item["html_path"],
        }
        if errors:
            rejected.append({**result, "errors": errors})
        else:
            reports.append({**result, "payload": payload})
    # Never combine reports checked under different rules to pass the gate.
    versions = {item["policy_version"] for item in reports}
    ready = len({item["company_id"] for item in reports}) >= 3 and len(versions) == 1
    return {
        "valid_count": len(reports),
        "required_count": 3,
        "ready_to_promote": ready,
        "reports": reports,
        "rejected": rejected,
        "active_policy_changed": False,
    }


def withdraw_staged_report(store, version: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("withdrawal requires a reason")
    _schema(store)
    with store.transaction() as db:
        row = db.execute(
            "SELECT * FROM research_report_staging WHERE version=?",
            (version,),
        ).fetchone()
        if not row:
            raise KeyError(version)
        promoted = db.execute(
            "SELECT version FROM publications WHERE version=? AND status='published'",
            (version,),
        ).fetchone()
        if promoted:
            # Use the store's existing revocation/correction implementation in
            # this transaction so a concurrent newer publication is not revoked.
            store._revoke(db, row["company_id"], reason)
        _retire_artifacts(row, "revoked", reason)
        _invalidate_notifications(db, row, reason)
        db.execute(
            "UPDATE research_report_staging SET status='revoked' WHERE version=?",
            (version,),
        )
        db.execute(
            "INSERT INTO events(company_id,kind,payload,created_at) "
            "VALUES(?,'research_report_withdrawn',?,?)",
            (
                row["company_id"],
                canonical({"version": version, "reason": reason}),
                utcnow(),
            ),
        )


def revalidate_staging(store) -> dict:
    """Invalidate stale artifacts and catch acknowledgements racing withdrawal."""
    status = release_status(store)
    for rejected in status["rejected"]:
        reason = "; ".join(error["code"] for error in rejected["errors"])
        withdraw_staged_report(store, rejected["version"], reason)
    with store.transaction() as db:
        retired = db.execute(
            "SELECT * FROM research_report_staging WHERE status IN ('revoked','superseded')"
        ).fetchall()
        for row in retired:
            _invalidate_notifications(db, row, "已撤回或被替代的报告收到延迟发送回执")
    return release_status(store)


def revalidate_published_research(store, as_of: date | str) -> dict:
    """Revoke stale adaptive claims before report/Bot reads, without model calls."""
    from .research_contracts import assess_coverage

    current_date = as_of if isinstance(as_of, date) else date.fromisoformat(as_of)
    revoked, checked = [], 0
    with store.transaction() as db:
        rows = db.execute(
            "SELECT p.version,p.company_id,p.payload,c.input_hash AS current_hash,"
            "c.stage AS company_stage FROM publications p LEFT JOIN companies c "
            "ON c.id=p.company_id WHERE p.status='published' "
            "AND json_extract(p.payload,'$.policy_version') IN ('model-led-research/1','raw-pdf-sessions/1')"
        ).fetchall()
        for row in rows:
            checked += 1
            publication = json.loads(row["payload"])
            reasons = [error["code"] for error in _acceptance(publication)]
            if row["current_hash"] != publication.get("input_hash"):
                reasons.append("published_evidence_changed")
            if row["company_stage"] == "excluded":
                reasons.append("published_issuer_excluded")
            packet = publication.get("evidence_packet") or {}
            if packet.get("company", {}).get("id") != row["company_id"]:
                reasons.append("published_identity_mismatch")
            coverage = (assess_coverage(packet, current_date)["coverage"]
                        if publication.get("policy_version") == "model-led-research/1"
                        else {})
            for key, item in coverage.items():
                if item["stale_metrics"]:
                    reasons.append(f"published_stale_{key}")
            # assess_coverage checks the actual market-cap observation, not an
            # untrusted quote_date label. Price, if present, must also be current.
            prices = [
                fact.get("period_end")
                for fact in packet.get("facts", [])
                if fact.get("metric") == "price"
            ]
            if prices:
                try:
                    latest_price = max(date.fromisoformat(value) for value in prices)
                    if not 0 <= (current_date - latest_price).days <= 7:
                        reasons.append("published_price_expired")
                except (TypeError, ValueError):
                    reasons.append("published_price_date_invalid")
            if not reasons:
                continue
            reason = "; ".join(sorted(set(reasons)))
            store._revoke(db, row["company_id"], reason)
            revoked.append(
                {
                    "company_id": row["company_id"],
                    "version": row["version"],
                    "reason": reason,
                }
            )
    # Keep direct staging links and any earlier staging notification aligned.
    if revoked:
        _schema(store)
        with store.transaction() as db:
            staged_versions = {
                row[0]
                for row in db.execute("SELECT version FROM research_report_staging")
            }
        for item in revoked:
            if item["version"] in staged_versions:
                withdraw_staged_report(store, item["version"], item["reason"])
    return {"as_of": current_date.isoformat(), "checked": checked, "revoked": revoked}


def _coverage(row: dict) -> dict | None:
    """Count existing evidence, not profitability or promised readiness."""
    company = row.get("company") or {}
    packet = row.get("packet") or {}
    if row.get("stage") == "excluded" or company.get("listing_status") in {
        "inactive",
        "out_of_universe",
    }:
        return None
    issuer = row.get("id") or company.get("id")
    if not issuer:
        return None
    sources = {
        source["id"]: source
        for source in packet.get("sources", [])
        if source.get("id")
        and source.get("text")
        and source.get("kind") in {"annual", "interim", "prospectus"}
        and source.get("issuer_id") == issuer
    }
    facts = [
        fact for fact in packet.get("facts", []) if fact.get("source_id") in sources
    ]
    annual_years = sorted(
        {
            str(fact.get("period_end", ""))[:4]
            for fact in facts
            if fact.get("period_type") == "annual"
            and len(str(fact.get("period_end", ""))) == 10
        }
    )
    metrics = sorted({str(fact.get("metric")) for fact in facts if fact.get("metric")})
    grade = (
        "extended"
        if len(annual_years) >= 7
        else ("recent" if len(annual_years) >= 3 else "limited")
    )
    return {
        "company_id": issuer,
        "name": company.get("name", issuer),
        "template": company.get("template", "ordinary"),
        "input_hash": row.get("input_hash") or fingerprint(packet),
        "coverage_grade": grade,
        "primary_fact_count": len(facts),
        "primary_page_count": len(sources),
        "annual_years": annual_years,
        "metric_count": len(metrics),
        "metrics": metrics,
        "limited_history_path": len(annual_years) < 7,
        "selection_reason": "existing_primary_evidence_coverage_not_profitability",
        "ready_for_report": False,
    }


def freeze_manifest(companies: list[dict], *, as_of: str | None = None) -> dict:
    """Select three primary and three reserve issuers without financial screening.

    This pure function returns a manifest for the caller to freeze in its ledger.
    Presence/counting is a scheduling aid, never evidence validation.
    """
    candidates = [item for row in companies if (item := _coverage(row))]
    candidates = [item for item in candidates if item["primary_page_count"]]
    candidates.sort(
        key=lambda item: (
            -item["metric_count"],
            -item["primary_fact_count"],
            -item["primary_page_count"],
            item["company_id"],
        )
    )
    unique = {item["company_id"]: item for item in reversed(candidates)}
    candidates = sorted(
        unique.values(),
        key=lambda item: (
            -item["metric_count"],
            -item["primary_fact_count"],
            -item["primary_page_count"],
            item["company_id"],
        ),
    )
    main = candidates[:1]
    if main:
        diverse = next(
            (
                item
                for item in candidates[1:]
                if item["coverage_grade"] != main[0]["coverage_grade"]
            ),
            None,
        )
        if diverse:
            main.append(diverse)
    for item in candidates:
        if len(main) == 3:
            break
        if item not in main:
            main.append(item)
    # If the diversity pick still has seven years, require a limited-history
    # path rather than claiming that future estimates already exist.
    if main and not any(item["limited_history_path"] for item in main):
        limited = next(
            (item for item in candidates if item["limited_history_path"]), None
        )
        if limited:
            main[-1] = limited
    reserve = [item for item in candidates if item not in main][:3]
    unmet = []
    if len(main) < 3 or len(reserve) < 3:
        unmet.append("fewer_than_six_issuers_with_cached_primary_pages")
    if len({item["coverage_grade"] for item in main}) < 2:
        unmet.append("two_coverage_grades_not_available")
    if not any(item["limited_history_path"] for item in main):
        unmet.append("limited_history_or_estimate_path_not_selected")
    manifest = {
        "schema_version": "research-manifest/1",
        "as_of": as_of or datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        "main": main,
        "reserve": reserve,
        "pool_count": len(companies),
        "cached_primary_issuer_count": len(candidates),
        "coverage_requirements_met": not unmet,
        "unmet_requirements": unmet,
        "selection_is_acceptance": False,
        "replacement_rule": "documented_unresolvable_material_evidence_gap_only",
        "required_conclusions": sorted(CONCLUSION_KEYS),
    }
    return {**manifest, "manifest_hash": fingerprint(manifest)}
