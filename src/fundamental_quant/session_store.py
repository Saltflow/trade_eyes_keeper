"""Persistent PDF sessions: active context is replaceable, history is not."""

from __future__ import annotations

import json
from copy import deepcopy

from .contracts import canonical, fingerprint
from .store import utcnow


class SessionStore:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pdf_research_sessions (
                id TEXT PRIMARY KEY, company_id TEXT NOT NULL, campaign_id TEXT,
                revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS pdf_session_history (
                session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, sequence)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS pdf_session_rounds (
                session_id TEXT NOT NULL, round INTEGER NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, round)
            )""")

    def load(self, session_id):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT state FROM pdf_research_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state, *, kind, detail=None, round_result=None):
        """CAS and history/round checkpoint commit in the same transaction."""
        state = deepcopy(state)
        expected = state.get("revision", 0)
        state["revision"] = expected + 1
        state["updated_at"] = utcnow()
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT revision FROM pdf_research_sessions WHERE id=?", (state["id"],)
            ).fetchone()
            if previous and previous[0] != expected:
                raise RuntimeError("session_revision_changed")
            if not previous and expected:
                raise RuntimeError("session_checkpoint_missing")
            db.execute(
                "INSERT INTO pdf_research_sessions VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,"
                "state=excluded.state,updated_at=excluded.updated_at",
                (
                    state["id"],
                    state["company"]["id"],
                    state.get("campaign_id"),
                    state["revision"],
                    canonical(state),
                    state["updated_at"],
                ),
            )
            db.execute(
                "INSERT INTO pdf_session_history VALUES(?,?,?,?,?)",
                (
                    state["id"],
                    state["revision"],
                    kind,
                    canonical(detail if detail is not None else {}),
                    state["updated_at"],
                ),
            )
            if round_result is not None:
                db.execute(
                    "INSERT INTO pdf_session_rounds VALUES(?,?,?,?)",
                    (state["id"], state["round"], canonical(round_result), utcnow()),
                )
        return state

    def history(self, session_id):
        with self.store.transaction() as db:
            return [
                dict(row, payload=json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT * FROM pdf_session_history WHERE session_id=? ORDER BY sequence",
                    (session_id,),
                )
            ]

    def summaries(self):
        with self.store.transaction() as db:
            states = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT state FROM pdf_research_sessions ORDER BY updated_at"
                )
            ]
        result = {}
        for state in states:
            summary = {
                k: state.get(k)
                for k in (
                    "id",
                    "campaign_id",
                    "phase",
                    "round",
                    "updated_at",
                    "coverage",
                    "last_reviewed",
                    "errors",
                    "publication_version",
                    "report_path",
                )
            }
            with self.store.transaction() as db:
                company = db.execute(
                    "SELECT input_hash FROM companies WHERE id=?",
                    (state["company"]["id"],),
                ).fetchone()
                staging_exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='research_report_staging'"
                ).fetchone()
                publication = (
                    db.execute(
                        "SELECT status FROM research_report_staging WHERE version=?",
                        (state["publication_version"],),
                    ).fetchone()
                    if staging_exists and state.get("publication_version")
                    else None
                )
            stale = bool(
                state.get("reviewed_evidence_hash")
                and (not company or state["reviewed_evidence_hash"] != company[0])
            )
            retired = bool(
                state.get("publication_version")
                and (not publication or publication[0] != "staged")
            )
            if stale or retired:
                from .reporting import _atomic, _document

                reason = (
                    "原始证据已改变，等待重新审查" if stale else "报告已撤回或被替代"
                )
                summary.update(
                    last_reviewed=None,
                    phase="stale",
                    errors=[
                        {"code": "session_result_no_longer_current", "reason": reason}
                    ],
                )
                path = self.store.root / "sessions" / (state["id"] + ".html")
                _atomic(
                    path,
                    _document(
                        "研究阶段结果已失效",
                        "<p>" + reason + "。原有审计记录保留。</p>",
                    ),
                )
            result[state["company"]["id"]] = summary
        return result


def session_id(issuer, campaign_id, version):
    return fingerprint({"company": issuer, "campaign": campaign_id, "version": version})
