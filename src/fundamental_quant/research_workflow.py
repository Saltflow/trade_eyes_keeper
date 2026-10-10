"""Staged model-led research, charged to existing durable authorization only."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

from .calculations import accounting_checks, screen
from .contracts import canonical, fingerprint, issue, validate_packet
from .research_calculations import calculate_research
from .research_contracts import (
    assess_coverage,
    report_acceptance,
    validate_decision,
    validate_estimates,
)
from .research_prompts import VERSION
from .research_release import release_status, stage_report
from .store import utcnow


class ResearchWorkflow:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def _packet(self, issuer):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT packet FROM companies WHERE id=?", (issuer,)
            ).fetchone()
        if not row or not row[0]:
            raise ValueError("cached issuer packet unavailable")
        return json.loads(row[0])

    def prepare(self):
        """Freeze existing evidence without collection, model calls or activation."""
        previous = self.store.events(kind="research_acceptance_manifest")
        if previous and previous[-1]["payload"].get("policy_version") == VERSION:
            return previous[-1]["payload"]
        # Stream heavy source packets; manifest retains only compact coverage.
        from .research_release import _coverage

        summaries = []
        for row in self.store.companies(include_packet=False):
            try:
                row["packet"] = self._packet(row["id"])
            except ValueError:
                continue
            summary = _coverage(row)
            if summary:
                summaries.append(summary)
        # Reuse the selector with compact representative source/fact records.
        # The actual immutable snapshot remains in the evidence ledger.
        manifest = self._select_manifest(summaries)
        manifest.update(
            policy_version=VERSION,
            as_of=str(self.service.today),
            acceptance_definition="three_substantive_independently_reviewed_reports",
        )
        self.store.event("", "research_acceptance_manifest", manifest)
        return manifest

    @staticmethod
    def _select_manifest(summaries):
        ordered = sorted(
            (s for s in summaries if s["primary_page_count"]),
            key=lambda s: (
                -s["metric_count"],
                -s["primary_fact_count"],
                s["company_id"],
            ),
        )
        selected = ordered[:3]
        grades = {s["coverage_grade"] for s in selected}
        alternate = next(
            (s for s in ordered[3:] if s["coverage_grade"] not in grades), None
        )
        if len(grades) < 2 and alternate:
            selected[-1] = alternate
        used = {s["company_id"] for s in selected}
        reserve = [s for s in ordered if s["company_id"] not in used][:3]
        diverse = len({s["coverage_grade"] for s in selected}) >= 2
        return {
            "main": selected,
            "reserve": reserve,
            "pool_coverage": summaries,
            "coverage_requirements_met": diverse
            and any(s["limited_history_path"] for s in selected),
            "limitations": [] if diverse else ["two_evidence_grades_not_yet_available"],
            "required_reports": 3,
            "frozen_at": utcnow(),
        }

    def preflight(self, targets=()):
        state = self.service.acceptance.state()
        problems = []
        activated = any(
            e["payload"].get("version") == VERSION
            for e in self.store.events(kind="research_policy_activated")
        )
        if not state.get("enabled") and activated:
            budget = self.service._model_budget_state()
            state = {
                "enabled": False,
                "status": "active",
                "limits": budget["limits"],
                "usage": budget["usage"],
                "id": None,
                "expires_at": None,
            }
        elif not state.get("enabled") or state.get("status") != "active":
            problems.append(
                issue(
                    "active_explicit_research_budget_required",
                    message="Existing stopped campaign is not automatically resumed",
                )
            )
        elif state.get("expires_at") and datetime.now(
            timezone.utc
        ) >= datetime.fromisoformat(state["expires_at"]):
            problems.append(issue("acceptance_time_limit"))
        for key, limit in state.get("limits", {}).items():
            if state.get("usage", {}).get(key, 0) >= limit:
                problems.append(issue("acceptance_budget_exhausted", key))
        if (
            not self.service.settings.get("llm", {}).get("enabled")
            and self.service._llm is None
        ):
            problems.append(issue("research_model_disabled"))
        if self.service._llm is None and not self.service.config.get("llm", {}).get(
            "api_key"
        ):
            import os

            if not os.getenv("DEEPSEEK_API_KEY"):
                problems.append(issue("credentials_missing"))
        outside = (
            sorted(set(targets) - set(state.get("targets", [])))
            if state.get("enabled")
            else []
        )
        if outside:
            problems.append(
                issue("targets_outside_authorized_campaign", message=canonical(outside))
            )
        return {
            "status": "blocked" if problems else "ready",
            "errors": problems,
            "campaign_id": state.get("id"),
            "usage": state.get("usage", {}),
            "limits": state.get("limits", {}),
            "expires_at": state.get("expires_at"),
        }

    def _checkpoint(self, packet=None):
        from .service import ResearchBlocked

        campaign_id = self.service.acceptance.id
        starts = [
            e for e in self.store.events(kind="research_acceptance_started")
            if e["payload"].get("campaign_id") == campaign_id
        ]
        if not starts:
            self.store.event(
                "",
                "research_acceptance_started",
                {
                    "policy_version": VERSION,
                    "started_at": utcnow(),
                    "campaign_id": self.service.acceptance.id,
                },
            )
            return
        start = next(
            (
                e["payload"]
                for e in reversed(starts)
                if e["payload"].get("policy_version") == VERSION
            ),
            None,
        )
        if not start:
            self.store.event(
                "",
                "research_acceptance_started",
                {
                    "policy_version": VERSION,
                    "started_at": utcnow(),
                    "campaign_id": self.service.acceptance.id,
                },
            )
            return
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(start["started_at"])
        ).total_seconds()
        if (
            elapsed >= 1800
            and not release_status(self.store, policy_version=VERSION)["valid_count"]
        ):
            signature = fingerprint(
                {
                    "versions": self.service._versions(),
                    "evidence": {
                        k: (packet or {}).get(k)
                        for k in ("company", "facts", "sources")
                    },
                }
            )
            old = [
                e for e in self.store.events(kind="research_checkpoint_failed")
                if e["payload"].get("campaign_id") == campaign_id
            ]
            repairs = [
                e for e in self.store.events(kind="research_checkpoint_repair")
                if e["payload"].get("campaign_id") == campaign_id
            ]
            if old and signature != old[-1]["payload"].get("signature"):
                if any(e["payload"].get("signature") == signature for e in repairs):
                    return
                if len(repairs) < 2:
                    self.store.event(
                        "",
                        "research_checkpoint_repair",
                        {
                            "campaign_id": campaign_id,
                            "signature": signature,
                            "original_started_at": start["started_at"],
                            "budget_reset": False,
                        },
                    )
                    return
            failure = {
                "campaign_id": campaign_id,
                "policy_version": VERSION,
                "code": "first_report_checkpoint_failed",
                "started_at": start["started_at"],
                "usage": self.service.acceptance.state().get("usage", {}),
                "signature": signature,
            }
            if not any(e["payload"].get("signature") == signature for e in old):
                self.store.event("", "research_checkpoint_failed", failure)
            self.store.enqueue("research_blocked", failure)
            self.service._deliver()
            raise ResearchBlocked(
                [
                    issue(
                        "first_report_checkpoint_failed",
                        message="30 minutes elapsed without a valid report; repair before resuming",
                    )
                ]
            )

    def _call(self, role, packet, extra=None):
        self._checkpoint(packet)
        return self.service._call(role, packet, extra)

    def _usage(self, issuer):
        events = self.store.events(company_id=issuer)
        rows = [
            e["payload"]
            for e in events
            if e["kind"] in {"model_response", "model_error"}
        ]

        def known_tokens(row, key):
            value = (row.get("usage") or {}).get(key)
            return value if type(value) is int and value >= 0 else None

        return {
            "model_calls": sum(e["kind"] == "model_request" for e in events),
            "input_tokens": sum(
                value for r in rows
                if (value := known_tokens(r, "input_tokens")) is not None
            ),
            "output_tokens": sum(
                value for r in rows
                if (value := known_tokens(r, "output_tokens")) is not None
            ),
            "scope": "issuer_lifetime_recorded_usage_including_failed_calls",
            "unknown_usage_calls": sum(
                any(known_tokens(r, key) is None
                    for key in ("input_tokens", "output_tokens"))
                for r in rows
            ),
        }

    def research(self, issuer):
        from .service import ResearchBlocked, evidence_hash

        check = self.preflight([issuer])
        if check["errors"]:
            raise ResearchBlocked(check["errors"])
        campaign = self.service.acceptance.state()
        if campaign.get("enabled") and issuer not in campaign.get("targets", []):
            raise ResearchBlocked([issue("issuer_outside_authorized_campaign", issuer)])
        packet = self._packet(issuer)
        evidence_key = evidence_hash(packet, exclude_prices=True)
        root_failures = self.store.events(
            kind="research_root_failure", company_id=issuer
        )
        counts = {}
        for failure in root_failures:
            if failure["payload"].get("evidence_key") == evidence_key:
                for code in failure["payload"].get("codes", []):
                    counts[code] = counts.get(code, 0) + 1
        if any(count >= 3 for count in counts.values()):
            raise ResearchBlocked([issue("research_root_repair_limit", issuer)])
        facts, errors = validate_packet(packet, self.service.today)
        packet = {**packet, "facts": facts}
        previous_exclusions = packet.get("research_coverage", {}).get(
            "excluded_fact_errors", []
        )
        packet["research_coverage"] = assess_coverage(packet, self.service.today)
        packet["research_coverage"]["excluded_fact_errors"] = (
            errors or previous_exclusions
        )
        retry_key = fingerprint(
            {"packet": packet, "policy": VERSION, "versions": self.service._versions()}
        )
        previous = self.store.events(kind="research_attempt", company_id=issuer)
        if any(e["payload"].get("key") == retry_key for e in previous):
            raise ResearchBlocked(
                [issue("unchanged_research_attempt_requires_repair", issuer)]
            )
        rejected = self.store.events(kind="research_report_rejected", company_id=issuer)
        if (
            sum(
                e["payload"].get("evidence_hash") == fingerprint(packet)
                for e in rejected
            )
            >= 3
        ):
            raise ResearchBlocked([issue("research_repair_replays_exhausted", issuer)])
        self.store.event(
            issuer, "research_attempt", {"key": retry_key, "policy_version": VERSION}
        )
        # Rejected observations are not silently repaired or used by calculations.
        self.store.event(
            issuer,
            "research_input_check",
            {"errors": errors, "coverage": packet["research_coverage"]},
        )
        decision = self._call(
            "research_decide",
            packet,
            {
                "reference_screen": screen(
                    packet, self.service.today, self.service.settings
                ),
                "recent_extraction_findings": [
                    {
                        "pages": e["payload"].get("pages", []),
                        "issues": e["payload"].get("issues", []),
                        "rejected_proposals": e["payload"].get(
                            "rejected_proposals", []
                        ),
                        "accepted_fact_ids": [
                            f["id"] for f in e["payload"].get("facts", [])
                        ],
                    }
                    for e in self.store.events(kind="extraction", company_id=issuer)[-3:]
                ],
            },
        )
        estimates = decision.get("estimates", [])
        problems = validate_estimates(estimates, packet)
        problems += validate_decision(
            decision.get("decision"),
            packet,
            [e.get("id") for e in estimates if isinstance(e, dict)],
        )
        if problems:
            raise ResearchBlocked(problems)
        choice = decision["decision"]
        self.store.event(issuer, "research_decision", decision)
        # A company researcher may estimate earnings while explicitly requesting
        # the cash-flow evidence needed for the rest of its report. Honor those
        # requests instead of discarding them merely because action=estimate.
        # Optional requests do not delay an otherwise adequately supported report.
        followup_fetch = False
        if choice.get("evidence_requests") and choice["action"] not in {
            "fetch",
            "defer",
            "abandon",
        }:
            from .research_contracts import coverage_with_estimates

            template = packet["company"]["template"]
            needed = (
                {"normalized_affo"} if template == "reit" else {"normalized_earnings"}
            )
            if template in {"ordinary", "cyclical"}:
                needed.add("normalized_fcf")
            proposed = {e["metric"] for e in estimates}
            coverage = coverage_with_estimates(packet, estimates, self.service.today)
            keys = {"normalized_earnings", "capital_efficiency", "implied_return"}
            if template not in {"bank", "insurance"}:
                keys.add("normalized_cashflow")
            followup_fetch = bool(needed - proposed) or any(
                coverage["coverage"][key]["status"] == "insufficient" for key in keys
            )
        if choice["action"] in {"fetch", "defer"} or followup_fetch:
            extraction_errors = []
            changed = False
            if choice["action"] == "fetch" or followup_fetch:
                before = fingerprint(packet)
                extraction_packet = {
                    **packet,
                    "research_mode": "model_led",
                    "research_evidence_requests": choice.get("evidence_requests", []),
                }
                original = self.service.settings.get("screen_first_extraction", True)
                try:
                    self.service.settings["screen_first_extraction"] = False
                    accepted, extraction_errors = self.service._extract(
                        extraction_packet, facts, []
                    )
                finally:
                    self.service.settings["screen_first_extraction"] = original
                packet["facts"] = accepted
                changed = before != fingerprint(packet)
                self.store.update(
                    issuer, "evidence", packet=packet, errors=extraction_errors
                )
                self.store.event(
                    issuer,
                    "research_fetch_result",
                    {
                        "before": before,
                        "after": fingerprint(packet),
                        "errors": extraction_errors,
                        "decision": choice,
                        "next_action": "reassess_changed_evidence",
                    },
                )
            return {
                "status": "deferred",
                "decision": choice,
                "company_id": issuer,
                "errors": extraction_errors,
                "evidence_changed": changed,
                "followup_action": "fetch" if followup_fetch else choice["action"],
            }
        metrics, calc_errors = calculate_research(packet, estimates)
        calc_errors += accounting_checks(packet)
        if calc_errors:
            raise ResearchBlocked(calc_errors)
        from .research_contracts import coverage_with_estimates

        packet["research_coverage"] = coverage_with_estimates(
            packet, estimates, self.service.today
        )
        template = packet["company"]["template"]
        required_metrics = (
            {"normalized_affo", "affo_yield"}
            if template == "reit"
            else {"normalized_earnings", "normalized_roe", "implied_ke_pb"}
            if template in {"bank", "insurance"}
            else {"normalized_earnings", "normalized_fcf", "fcf_yield", "implied_ke_pb"}
        )
        missing_metrics = sorted(required_metrics - metrics.keys())
        core = {"normalized_earnings", "capital_efficiency", "implied_return"}
        if template not in {"bank", "insurance"}:
            core.add("normalized_cashflow")
        missing_anchors = [
            k
            for k in core
            if packet["research_coverage"]["coverage"][k]["status"] == "insufficient"
        ]
        if missing_metrics or missing_anchors:
            assessment = {
                "status": "limited_assessment_not_acceptance_report",
                "company_id": issuer,
                "decision": choice,
                "missing_calculations": missing_metrics,
                "missing_anchors": missing_anchors,
                "metrics": metrics,
                "next_action": "targeted_evidence_or_supported_estimates_required",
            }
            self.store.event(issuer, "research_limited_assessment", assessment)
            return assessment
        packet_for_model = {**packet, "calculations": metrics}
        # Analyst output is absent from the blind context.
        blind = self._call("research_blind", packet)
        from .research_contracts import reference_conditions

        references = reference_conditions(packet, metrics, self.service.today)
        draft = self._call(
            "research_analyze",
            packet_for_model,
            {
                "decision": choice,
                "estimates": estimates,
                "reference_conditions": references,
            },
        )
        choice = draft.get("decision", choice)
        problems = validate_decision(choice, packet, [e["id"] for e in estimates])
        if problems:
            raise ResearchBlocked(problems)
        audit = self._call(
            "research_crosscheck",
            packet_for_model,
            {"decision": choice, "estimates": estimates, "draft": draft},
        )
        payload = {
            "company": packet["company"],
            "as_of": str(self.service.today),
            "policy_version": VERSION,
            "input_hash": fingerprint(packet),
            "evidence_packet": packet,
            "coverage": packet["research_coverage"],
            "estimates": estimates,
            "decision": choice,
            "reference_conditions": references,
            "metrics": metrics,
            "conclusions": draft.get("conclusions"),
            "breakers": draft.get("breakers", []),
            "candidate": choice["action"] == "candidate",
            "qualification": {
                "candidate": choice["action"] == "candidate",
                "policy_exceptions": choice["policy_exceptions"],
                "estimate_dependent": bool(estimates),
            },
            "review": {
                "blind": blind,
                "crosscheck": audit,
                "independence": "separate_contexts_not_independent_error_probabilities",
            },
            "usage": self._usage(issuer),
            "deterministic_errors": [],
            "quote_date": max(
                (f["period_end"] for f in facts if f["metric"] == "market_cap"),
                default="",
            ),
            "source_refs": [
                {k: s.get(k) for k in ("id", "url", "page", "hash", "origin_id")}
                for s in packet["sources"]
            ],
        }
        problems = report_acceptance(payload)
        if problems:
            # One correction of interpretation/schema on the same frozen evidence.
            # This cannot repair a missing original or change estimate assumptions.
            source_block = {
                "recent_primary_anchor_missing",
                "deterministic_verification_failed",
                "calculation_recompute_mismatch",
                "estimate_bounds_invalid",
            }
            if not any(p["code"] in source_block for p in problems):
                repaired = self._call(
                    "research_analyze",
                    packet_for_model,
                    {
                        "decision": choice,
                        "estimates": estimates,
                        "reference_conditions": references,
                        "previous_draft": draft,
                        "findings": problems,
                        "correction": "one_per_frozen_evidence",
                    },
                )
                final_choice = repaired.get("decision", choice)
                final_errors = validate_decision(
                    final_choice, packet, [e["id"] for e in estimates]
                )
                if not final_errors:
                    audit = self._call(
                        "research_crosscheck",
                        packet_for_model,
                        {
                            "decision": final_choice,
                            "estimates": estimates,
                            "draft": repaired,
                        },
                    )
                    payload.update(
                        decision=final_choice,
                        conclusions=repaired.get("conclusions"),
                        breakers=repaired.get("breakers", []),
                        candidate=final_choice["action"] == "candidate",
                        qualification={
                            "candidate": final_choice["action"] == "candidate",
                            "policy_exceptions": final_choice["policy_exceptions"],
                            "estimate_dependent": bool(estimates),
                        },
                    )
                    payload["review"]["crosscheck"] = audit
                    payload["usage"] = self._usage(issuer)
                    problems = report_acceptance(payload)
                else:
                    problems = final_errors
        if problems:
            self.store.event(
                issuer,
                "research_report_rejected",
                {
                    "errors": problems,
                    "draft": payload,
                    "evidence_hash": fingerprint(packet),
                    "version": VERSION,
                },
            )
            raise ResearchBlocked(problems)
        self.store.update(issuer, "reviewed", packet=packet)
        staged = stage_report(self.store, payload)
        self.store.enqueue(
            "research_report_ready",
            {
                "company_id": issuer,
                "staging_version": staged["version"],
                "report": payload,
            },
        )
        self.service._deliver()
        return {"status": "reviewed", **staged}

    def allocate(self, limit=20):
        """Round-robin whole-pool consideration with a durable exploration quota."""
        rows = [
            r
            for r in self.store.companies(include_packet=False)
            if r["stage"] != "excluded"
        ]
        history = self.store.events(kind="research_pool_cursor")
        cursor = history[-1]["payload"]["next_cursor"] if history else 0
        chunks = []
        cards = {}
        # Bound each cycle; record which fraction was considered, not full-pool completion.
        for offset in range(0, min(len(rows), 200), 100):
            group = [
                rows[(cursor + offset + n) % len(rows)]
                for n in range(min(100, len(rows) - offset))
            ]
            summaries = [
                {
                    "company_id": r["id"],
                    "company": r["company"],
                    "stage": r["stage"],
                    "screen_reference": r["screen"],
                    "errors": r["errors"],
                    "updated_at": r["updated_at"],
                }
                for r in group
            ]
            for card in summaries:
                try:
                    cached = self._packet(card["company_id"])
                except ValueError:
                    cached = {"facts": [], "sources": []}
                sources = {s["id"]: s for s in cached["sources"]}
                recent = {}
                for fact in cached["facts"]:
                    if fact["metric"] not in {
                        "market_cap",
                        "pe_ttm",
                        "pb",
                        "revenue",
                        "adjusted_net_income_parent",
                        "operating_cash_flow",
                        "capital_expenditures",
                        "parent_equity",
                        "total_liabilities",
                    }:
                        continue
                    old = recent.get(fact["metric"])
                    if old and old["period_end"] >= fact["period_end"]:
                        continue
                    recent[fact["metric"]] = {
                        k: fact.get(k)
                        for k in (
                            "value",
                            "period_end",
                            "period_type",
                            "currency",
                            "unit",
                            "source_id",
                        )
                    }
                    recent[fact["metric"]]["verification"] = sources.get(
                        fact["source_id"], {}
                    ).get("verification_level", "unreviewed")
                card["observed_signals_not_reviewed_conclusions"] = recent
                card["known_cost"] = self._usage(card["company_id"])
                decisions = self.store.events(
                    kind="research_decision", company_id=card["company_id"]
                )
                card["previous_decision"] = (
                    decisions[-1]["payload"].get("decision") if decisions else None
                )
                cards[card["company_id"]] = card
            result = self._call(
                "research_allocate",
                {
                    "company": {"id": "research_pool"},
                    "facts": [],
                    "sources": [],
                    "universe_summaries": summaries,
                },
                {"limit": limit},
            )
            allowed = {r["id"] for r in group}
            selected, deferred = (
                result.get("selections", []),
                result.get("deferred", []),
            )
            seen = [r.get("company_id") for r in selected + deferred]
            if (
                len(selected) > limit
                or len(seen) != len(set(seen))
                or set(seen) != allowed
                or any(not r.get("reason") for r in selected + deferred)
                or any(
                    not all(
                        r.get(k)
                        for k in (
                            "expected_information_gain",
                            "downside",
                            "next_action",
                        )
                    )
                    for r in selected
                )
            ):
                raise ValueError("invalid pool allocation coverage")
            chunks.extend(selected)
            self.store.event("", "research_pool_allocation", result)
        # Merge cross-batch choices; batch order must not determine research priority.
        unique = list(dict.fromkeys(r["company_id"] for r in chunks))
        if len(unique) > limit:
            result = self._call(
                "research_allocate",
                {
                    "company": {"id": "research_pool_merge"},
                    "facts": [],
                    "sources": [],
                    "universe_summaries": [cards[key] for key in unique],
                },
                {"limit": limit},
            )
            selections, deferred = (
                result.get("selections", []),
                result.get("deferred", []),
            )
            seen = [r.get("company_id") for r in selections + deferred]
            if (
                len(selections) > limit
                or set(seen) != set(unique)
                or len(seen) != len(set(seen))
            ):
                raise ValueError("invalid merged allocation coverage")
            chunks = selections
            self.store.event("", "research_pool_merged", result)
        considered = min(len(rows), 200)
        self.store.event(
            "",
            "research_pool_cursor",
            {
                "next_cursor": (cursor + considered) % max(1, len(rows)),
                "considered": considered,
                "pool_size": len(rows),
            },
        )
        candidates = list(dict.fromkeys(r["company_id"] for r in chunks))
        # Deterministic exploration slots cannot be consumed by completeness ranking.
        explore = [
            r["id"]
            for r in sorted(rows, key=lambda r: (r["updated_at"], r["id"]))
            if r["stage"] in {"discovered", "blocked", "evidence"}
        ][: math.ceil(limit * 0.2)]
        priorities = self.service.settings.get("model_led", {}).get(
            "priority_issuers", []
        )
        allowed = {r["id"] for r in rows}
        priorities = [issuer for issuer in priorities if issuer in allowed]
        return list(dict.fromkeys(priorities + explore + candidates))[:limit]

    def promote(self, policy_version=VERSION):
        state = release_status(self.store, policy_version=policy_version)
        if not state["ready_to_promote"]:
            return {
                "status": "blocked",
                "errors": [issue("three_valid_reports_required")],
                "valid_reports": state["valid_count"],
            }
        # Activation marker is written only after all current reviewed rows exist.
        versions = []
        with self.store.transaction() as db:
            for report in state["reports"]:
                payload, issuer = report["payload"], report["company_id"]
                current = db.execute(
                    "SELECT input_hash,stage FROM companies WHERE id=?", (issuer,)
                ).fetchone()
                if (
                    not current
                    or current["input_hash"] != payload["input_hash"]
                    or current["stage"] == "excluded"
                ):
                    raise ValueError("Research evidence changed during release")
                version = fingerprint({"company": issuer, "payload": payload})
                old = db.execute(
                    "SELECT status FROM publications WHERE version=?", (version,)
                ).fetchone()
                if old and old[0] != "published":
                    raise ValueError("Cannot resurrect withdrawn research")
                for previous in db.execute(
                    "SELECT version FROM publications WHERE company_id=? AND status='published' AND version<>?",
                    (issuer, version),
                ).fetchall():
                    self.store._invalidate_pending(db, previous[0])
                db.execute(
                    "UPDATE publications SET status='superseded' WHERE company_id=? AND status='published' AND version<>?",
                    (issuer, version),
                )
                db.execute(
                    "INSERT OR IGNORE INTO publications VALUES(?,?,'published',?,?)",
                    (version, issuer, canonical(payload), utcnow()),
                )
                db.execute(
                    "UPDATE companies SET stage='published' WHERE id=?", (issuer,)
                )
                versions.append(version)
            db.execute(
                "INSERT INTO events(company_id,kind,payload,created_at) VALUES('','research_policy_activated',?,?)",
                (canonical({"version": policy_version, "publications": versions}), utcnow()),
            )
        self.service.report()
        return {"status": "activated", "publications": versions}

    def run(self):
        from .service import ResearchBlocked

        check = self.preflight()
        if check["errors"]:
            self.store.event("", "research_preflight_blocked", check)
            return check
        manifest = self.prepare()
        targets = [r["company_id"] for r in manifest["main"]]
        check = self.preflight(targets)
        if check["errors"]:
            self.store.event("", "research_preflight_blocked", check)
            return check
        active = any(
            e["payload"].get("version") == VERSION
            for e in self.store.events(kind="research_policy_activated")
        )
        if active and not self.service.acceptance.enabled:
            targets = self.allocate(
                self.service.settings.get("daily_research_limit", 20)
            )
        outcomes = []
        queue = list(targets)
        if self.service.acceptance.enabled:
            # Cohort membership remains frozen. Within it, newly repaired recent
            # primary anchors go first so we do not spend the review reserve on
            # serial decisions for issuers whose key evidence is still missing.
            readiness = {}
            for issuer in queue:
                coverage = assess_coverage(self._packet(issuer), self.service.today)
                readiness[issuer] = sum(
                    coverage["coverage"][key]["status"] == "available"
                    for key in (
                        "normalized_earnings",
                        "normalized_cashflow",
                        "capital_efficiency",
                        "implied_return",
                    )
                )
            queue.sort(key=lambda issuer: -readiness[issuer])
            self.store.event(
                "",
                "research_acceptance_queue",
                {
                    "campaign_id": self.service.acceptance.id,
                    "targets": queue,
                    "available_core_anchors": readiness,
                    "reason": "recent_primary_evidence_readiness_not_profitability",
                },
            )
        reserves = [
            r["company_id"] for r in manifest["reserve"] if r["company_id"] not in queue
        ]
        for issuer in queue:
            try:
                while True:
                    control = self.service.root / "worker" / "runtime" / "drain.json"
                    if control.exists():
                        request = json.loads(control.read_text(encoding="utf-8"))
                        if request.get("run_id") and request["run_id"] == os.getenv(
                            "FQ_RUN_ID"
                        ):
                            self.store.event(
                                "", "run_drained", {"run_id": request["run_id"]}
                            )
                            self.service.report()
                            return {
                                "status": "blocked",
                                "outcomes": outcomes,
                                "errors": [issue("research_drain_requested")],
                            }
                    result = self.research(issuer)
                    outcomes.append(result)
                    if not (
                        result.get("status") == "deferred"
                        and result.get(
                            "followup_action", result.get("decision", {}).get("action")
                        )
                        == "fetch"
                        and result.get("evidence_changed") is True
                    ):
                        break
                    if self.preflight([issuer])["errors"]:
                        break
                if (
                    result.get("status") == "limited_assessment_not_acceptance_report"
                    and reserves
                ):
                    replacement = reserves.pop(0)
                    if not self.preflight([replacement])["errors"]:
                        self.store.event(
                            issuer,
                            "research_target_replaced",
                            {
                                "replacement": replacement,
                                "reason": result["next_action"],
                                "missing_anchors": result.get("missing_anchors", []),
                                "missing_calculations": result.get(
                                    "missing_calculations", []
                                ),
                                "not_based_on_profitability": True,
                            },
                        )
                        queue.append(replacement)
            except ResearchBlocked as exc:
                self._record_failure(issuer, exc.errors)
                self.store.event(
                    issuer,
                    "research_blocked",
                    {"errors": exc.errors, "version": VERSION},
                )
                outcomes.append(
                    {"company_id": issuer, "status": "blocked", "errors": exc.errors}
                )
                self.store.enqueue("research_blocked", outcomes[-1])
                self.service._deliver()
            except Exception as exc:  # noqa: BLE001 - isolate issuer, persist failure
                failure = {
                    "company_id": issuer,
                    "status": "blocked",
                    "errors": [
                        issue("research_execution_failed", issuer, type(exc).__name__)
                    ],
                }
                self.store.event(issuer, "research_blocked", failure)
                self._record_failure(issuer, failure["errors"])
                self.store.enqueue("research_blocked", failure)
                self.service._deliver()
                outcomes.append(failure)
            if self.service.acceptance.enabled and outcomes:
                self.service.acceptance.checkpoint(issuer, outcomes[-1])
        release = self.promote()
        if release.get("status") != "activated":
            # Failed/substantive-but-incomplete runs must refresh the same HTML
            # status source as successful runs, not leave yesterday's state.
            self.service.report()
        elif self.service.acceptance.enabled:
            self.service.acceptance.finish("completed", "three_verified_reports")
        return {
            "status": "accepted"
            if release.get("status") == "activated"
            else "incomplete",
            "outcomes": outcomes,
            "release": release,
        }

    def _record_failure(self, issuer, errors):
        from .service import evidence_hash

        try:
            packet = self._packet(issuer)
        except ValueError:
            packet = {"company": {"id": issuer}, "facts": [], "sources": []}
        with self.store.transaction() as db:
            exists = db.execute(
                "SELECT 1 FROM companies WHERE id=?", (issuer,)
            ).fetchone()
        if exists:
            self.store.update(issuer, "blocked", errors=errors)
        self.store.event(
            issuer,
            "research_root_failure",
            {
                "evidence_key": evidence_hash(packet, exclude_prices=True),
                "codes": sorted({e["code"] for e in errors}),
                "versions": self.service._versions(),
                "policy_version": VERSION,
            },
        )
