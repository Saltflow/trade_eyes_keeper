"""Versioned peer-group reports; never insert investment candidates.

The immutable reviewed payload is shared by local HTML, Bot reads and email.
Outbound delivery is explicit and reuses the project's notifier adapter.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .contracts import canonical, fingerprint
from .reporting import _atomic, _document, _escape
from .store import utcnow

TABLE = "longitudinal_publications"
OUTBOX = "longitudinal_outbox"


def _schema(store):
    with store.transaction() as db:
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            version TEXT PRIMARY KEY, group_id TEXT NOT NULL, packet_hash TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL, html TEXT NOT NULL,
            html_hash TEXT NOT NULL, html_path TEXT NOT NULL,
            reason TEXT, created_at TEXT NOT NULL,
            email_html TEXT NOT NULL DEFAULT '', email_hash TEXT NOT NULL DEFAULT '')""")
        columns = {r[1] for r in db.execute(f"PRAGMA table_info({TABLE})")}
        for name in ("email_html", "email_hash"):
            if name not in columns:
                db.execute(
                    f"ALTER TABLE {TABLE} ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        db.execute(f"""CREATE TABLE IF NOT EXISTS {OUTBOX} (
            id TEXT PRIMARY KEY, version TEXT NOT NULL, kind TEXT NOT NULL,
            channel TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT,
            created_at TEXT NOT NULL)""")


def _exists(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        is not None
    )


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def publication_errors(packet, state):
    from .longitudinal_workflow import (
        EvidenceTools,
        digest,
        validate_result,
        validate_review,
    )

    errors = []
    packet_hash = packet.get("packet_hash")
    if (
        not packet_hash
        or packet_hash
        != digest({k: v for k, v in packet.items() if k != "packet_hash"})
        or state.get("packet_hash") != digest(packet)
    ):
        errors.append("publication_packet_hash_mismatch")
    effective = state.get("effective_packet")
    if effective is not None and (
        effective.get("spec") != packet.get("spec")
        or effective.get("packet_hash")
        != digest({k: v for k, v in effective.items() if k != "packet_hash"})
        or state.get("evidence_revision") != digest(effective)
        or not state.get("evidence_updates")
        or state["evidence_updates"][-1].get("new_hash") != digest(effective)
    ):
        errors.append("publication_incremental_evidence_mismatch")
    if state.get("status") != "accepted":
        errors.append("publication_workflow_not_accepted")
    if state.get("deterministic_errors") != []:
        errors.append("publication_deterministic_check_not_clear")
    result = state.get("approved_result")
    if not isinstance(result, dict) or result.get("final") is not True:
        return errors + ["publication_final_report_missing"]
    if state.get("research_result") != result:
        errors.append("publication_newer_unreviewed_result")
    evidence = EvidenceTools(packet, copy.deepcopy(state))
    try:
        errors.extend(validate_result(result, evidence))
        reviews = state.get("reviews") or []
        review = reviews[-1] if reviews else {}
        if review.get("verdict") != "pass" or review.get("validation_errors") != []:
            errors.append("publication_review_not_pass")
        if review.get("independence") != "separate_context_same_model":
            errors.append("publication_review_context_missing")
        if review.get("result_hash") != digest(result):
            errors.append("publication_review_result_mismatch")
        errors.extend(validate_review(review, result, evidence))
        if state.get("evidence_revision") != review.get("evidence_revision"):
            errors.append("publication_review_evidence_mismatch")
        original_review = {
            k: v
            for k, v in review.items()
            if k
            not in {
                "result_hash",
                "validation_errors",
                "independence",
                "evidence_revision",
            }
        }
        expected = digest(
            {
                "packet": state.get("packet_hash"),
                **(
                    {"evidence_revision": state["evidence_revision"]}
                    if state.get("evidence_revision")
                    else {}
                ),
                "result": result,
                "review": original_review,
            }
        )
        if state.get("report_version") != expected:
            errors.append("publication_review_version_mismatch")
    except (TypeError, KeyError, ValueError, AttributeError):
        errors.append("publication_contract_malformed")
    if not (packet.get("spec") or {}).get("group_id"):
        errors.append("publication_group_missing")
    return sorted(set(errors))


def _queue_correction(db, version):
    # Includes late acknowledgements after an in-flight message was withdrawn.
    rows = db.execute(
        f"SELECT channel,receipt FROM {OUTBOX} WHERE version=? AND kind='publication'",
        (version,),
    ).fetchall()
    for row in rows:
        receipt = json.loads(row["receipt"] or "{}")
        if receipt.get("status") not in {"sent", "delivered"}:
            continue
        oid = fingerprint([version, "correction", row["channel"]])
        db.execute(
            f"INSERT OR IGNORE INTO {OUTBOX} VALUES(?,?,?,?,'pending',NULL,?)",
            (oid, version, "correction", row["channel"], utcnow()),
        )


def _retire(db, row, status, reason):
    db.execute(
        f"UPDATE {TABLE} SET status=?,reason=? WHERE version=?",
        (status, reason, row["version"]),
    )
    db.execute(
        f"UPDATE {OUTBOX} SET status='cancelled' WHERE version=? "
        "AND kind='publication' AND status IN ('pending','failed','sending')",
        (row["version"],),
    )
    _queue_correction(db, row["version"])
    _atomic(
        Path(row["html_path"]),
        _document(
            "同行研究报告已撤回或被替代",
            "<p>原结论不再有效，历史内容保留在审计库。</p><p>版本："
            + _escape(row["version"])
            + "</p><p>原因："
            + _escape(reason)
            + "</p>",
        ),
    )


def publish_group_report(store, packet, state, *, renderer=None, channels=()):
    """Persist a reviewed snapshot and optionally queue notifications, never send."""
    errors = publication_errors(packet, state)
    if errors:
        raise ValueError("; ".join(errors))
    if renderer is None:
        from .longitudinal_report import render_longitudinal_report

        renderer = render_longitudinal_report
    base_packet = packet
    packet = state.get("effective_packet", packet)
    payload = json.loads(
        canonical(
            {
                "packet": packet,
                "state": state,
                **(
                    {"base_packet": base_packet}
                    if state.get("effective_packet")
                    else {}
                ),
            }
        )
    )
    group_id = packet["spec"]["group_id"]
    version = fingerprint(payload)
    html = renderer(packet, state)
    from .longitudinal_report import render_longitudinal_email

    email_html = render_longitudinal_email(packet, state, version)
    html_path = store.root / "longitudinal_reports" / (version + ".html")
    channels = sorted(set(channels))
    if any(c not in {"email", "feishu", "telegram"} for c in channels):
        raise ValueError("unsupported notification channel")
    _schema(store)
    with store.transaction() as db:
        current = db.execute(
            f"SELECT * FROM {TABLE} WHERE version=?", (version,)
        ).fetchone()
        if current and current["status"] != "published":
            raise ValueError("withdrawn version requires new evidence or review")
        if not current:
            for prior in db.execute(
                f"SELECT * FROM {TABLE} WHERE group_id=? AND status='published'",
                (group_id,),
            ).fetchall():
                _retire(db, prior, "superseded", "replaced by " + version)
            _atomic(html_path, html)
            db.execute(
                f"INSERT INTO {TABLE} VALUES(?,?,?,'published',?,?,?,?,NULL,?,?,?)",
                (
                    version,
                    group_id,
                    packet["packet_hash"],
                    canonical(payload),
                    html,
                    _hash(html),
                    str(html_path.resolve()),
                    utcnow(),
                    email_html,
                    _hash(email_html),
                ),
            )
            db.execute(
                "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,?,?,?)",
                (
                    group_id,
                    "longitudinal_report_published",
                    canonical({"version": version}),
                    utcnow(),
                ),
            )
        for channel in channels:
            oid = fingerprint([version, "publication", channel])
            db.execute(
                f"INSERT OR IGNORE INTO {OUTBOX} VALUES(?,?,?,?,'pending',NULL,?)",
                (oid, version, "publication", channel, utcnow()),
            )
    return get_group_report(store, version=version)


def _visible(row):
    if row is None or row["status"] != "published":
        return None
    payload = json.loads(row["payload"])
    path = Path(row["html_path"])
    if (
        fingerprint(payload) != row["version"]
        or publication_errors(
            payload.get("base_packet", payload["packet"]), payload["state"]
        )
        or _hash(row["html"]) != row["html_hash"]
        or not row["email_html"]
        or _hash(row["email_html"]) != row["email_hash"]
        or not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != row["html_hash"]
    ):
        return None
    return {
        "version": row["version"],
        "group_id": row["group_id"],
        "packet_hash": row["packet_hash"],
        "status": "published",
        "html_path": row["html_path"],
        "html": row["html"],
        "email_html": row["email_html"],
        "payload": payload,
        "candidate": False,
    }


def get_group_report(store, group_id=None, *, version=None):
    with store.transaction() as db:
        if not _exists(db):
            return None
        if version:
            row = db.execute(
                f"SELECT * FROM {TABLE} WHERE version=?", (version,)
            ).fetchone()
        else:
            row = db.execute(
                f"SELECT * FROM {TABLE} WHERE group_id=? ORDER BY rowid DESC LIMIT 1",
                (group_id,),
            ).fetchone()
    return _visible(row)


def list_group_reports(store, issuer=None):
    with store.transaction() as db:
        if not _exists(db):
            return []
        rows = db.execute(
            f"SELECT * FROM {TABLE} WHERE status='published' ORDER BY created_at DESC"
        ).fetchall()
    reports = [item for row in rows if (item := _visible(row))]
    if issuer:
        reports = [
            item
            for item in reports
            if issuer == item["group_id"]
            or any(
                issuer
                in {
                    c.get("id"),
                    c.get("issuer_id"),
                    c.get("code"),
                    str(c.get("issuer_id", "")).split(":")[-1],
                }
                for c in item["payload"]["packet"]["spec"].get("companies", [])
            )
        ]
    return reports


def group_bot_view(report):
    """The Bot reads exactly the accepted report; no new model judgement."""
    state = report["payload"]["state"]
    return {k: report[k] for k in ("version", "group_id", "status", "html_path")} | {
        "report": state["approved_result"],
        "review": state["reviews"][-1],
        "usage": state.get("usage", {}),
        "candidate": False,
    }


def withdraw_group_report(store, version, reason):
    if not reason.strip():
        raise ValueError("withdrawal reason required")
    with store.transaction() as db:
        if not _exists(db):
            raise KeyError(version)
        row = db.execute(
            f"SELECT * FROM {TABLE} WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise KeyError(version)
        _retire(db, row, "revoked", reason)
        db.execute(
            "INSERT INTO events(company_id,kind,payload,created_at) VALUES(?,?,?,?)",
            (
                row["group_id"],
                "longitudinal_report_withdrawn",
                canonical({"version": version, "reason": reason}),
                utcnow(),
            ),
        )


def deliver_group_notifications(store, config, *, manager=None):
    """Explicit dispatch; uncertain in-flight deliveries are never blindly retried."""
    from ..notification.settings import channel_enabled
    from .notifications import deliver_notification

    if (
        not config.get("fundamental_quant", {})
        .get("notifications", {})
        .get("enabled", False)
    ):
        return []
    with store.transaction() as db:
        if not _exists(db):
            return []
        rows = db.execute(
            f"SELECT * FROM {OUTBOX} WHERE status='pending' ORDER BY created_at"
        ).fetchall()
    results = []
    for notice in rows:
        channel = notice["channel"]
        if not channel_enabled(config, channel):
            continue
        with store.transaction() as db:
            claimed = db.execute(
                f"UPDATE {OUTBOX} SET status='sending' WHERE id=? AND status='pending'",
                (notice["id"],),
            ).rowcount
            if not claimed:
                continue
            row = db.execute(
                f"SELECT * FROM {TABLE} WHERE version=?", (notice["version"],)
            ).fetchone()
            if notice["kind"] == "publication":
                report = _visible(row)
                if not report:
                    db.execute(
                        f"UPDATE {OUTBOX} SET status='cancelled' WHERE id=?",
                        (notice["id"],),
                    )
                    continue
                body = report["email_html"]
                message = canonical(group_bot_view(report))
                title = "长期同行研究｜" + row["group_id"]
            else:
                message = (
                    "研究报告已撤回或被替代。版本："
                    + row["version"]
                    + "；原因："
                    + (row["reason"] or "")
                )
                body = _document("研究报告更正", "<p>" + _escape(message) + "</p>")
                title = "长期同行研究｜更正通知"
        response = deliver_notification(
            config,
            title,
            message,
            manager=manager,
            channels=[channel],
            html_content=body,
        )
        receipt = response.get("channels", {}).get(channel, {"status": "unknown"})
        with store.transaction() as db:
            sent = receipt.get("status") in {"sent", "delivered"}
            db.execute(
                f"UPDATE {OUTBOX} SET receipt=?,status=CASE WHEN status='cancelled' THEN status ELSE ? END WHERE id=?",
                (canonical(receipt), "delivered" if sent else "unknown", notice["id"]),
            )
            current = db.execute(
                f"SELECT status FROM {TABLE} WHERE version=?", (notice["version"],)
            ).fetchone()
            if (
                sent
                and notice["kind"] == "publication"
                and current["status"] != "published"
            ):
                _queue_correction(db, notice["version"])
        results.append({"id": notice["id"], "version": notice["version"], **response})
    return results
