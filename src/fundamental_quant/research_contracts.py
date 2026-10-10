"""Evidence contracts for model-directed research, independent of legacy screens.

Coverage is descriptive: missing history narrows a conclusion, not a company's
eligibility. Publication acceptance is stricter and requires frozen, validated
anchors, reproducible calculations and two evidence-bearing review contexts.
"""

from __future__ import annotations

import copy
import re
from datetime import date
from typing import TypedDict

from .calculations import ANNUAL_CORE, DERIVED_INPUTS, SPECIAL
from .contracts import (
    CONCLUSION_KEYS,
    CONCLUSION_STATUSES,
    TEMPLATES,
    _references,
    canonical,
    fingerprint,
    finite,
    issue,
    validate_packet,
)

VERSION = "fundamental-research-contracts/1"
ACTIONS = {
    "research",
    "fetch",
    "estimate",
    "narrow",
    "candidate",
    "observe",
    "defer",
    "abandon",
}
ESTIMATE_UNITS = {
    "normalized_earnings": "currency",
    "normalized_fcf": "currency",
    "normalized_affo": "currency",
    "normalized_roe": "ratio",
    "normalized_roic": "ratio",
}
REVIEW_TOPICS = {
    "earnings",
    "cashflow",
    "equity",
    "debt",
    "adjustments",
    "peers",
    "thesis",
}
INDEPENDENCE = "separate_contexts_not_independent_error_probabilities"
CORE_METRICS = {
    "normalized_earnings": {"normalized_earnings", "normalized_affo"},
    "normalized_cashflow": {"normalized_fcf", "normalized_affo"},
    "capital_efficiency": {"normalized_roe", "normalized_roic"},
    "implied_return": {"implied_ke_pb", "implied_ke_pe", "fcf_yield", "affo_yield"},
}
EVIDENCE_METRICS = (
    frozenset(ANNUAL_CORE)
    | {metric for group in SPECIAL.values() for metric in group}
    | {metric for group in DERIVED_INPUTS.values() for metric in group}
    | {
        "total_assets",
        "total_liabilities",
        "average_parent_equity",
        "average_invested_capital",
        "net_debt",
        "adjusted_roe",
        "reported_roe",
        "combined_ratio",
        "new_business_value",
        "asset_life_years",
        "nav_per_share",
        "basic_eps",
        "diluted_eps",
        "depreciation_amortization",
        "operating_working_capital",
        "minority_dividends_paid",
        "dividends_and_interest_paid",
        "interest_paid",
        "lease_cash_payments",
    }
)


class _RequiredDecision(TypedDict):
    action: str
    reason: str
    comparison_reason: str
    fact_refs: list[str]
    source_refs: list[str]
    estimate_refs: list[str]
    policy_exceptions: list[dict]
    next_steps: list[str]
    revisit_triggers: list[str]


class ResearchDecision(_RequiredDecision, total=False):
    evidence_requests: list[dict]


class Estimate(TypedDict):
    id: str
    metric: str
    period_end: str
    unit: str
    currency: str
    low: float
    base: float
    high: float
    method: str
    fact_refs: list[str]
    source_refs: list[str]
    counterevidence: list[str]
    sensitivity: list[str]
    invalidation: list[str]


class ConclusionCoverage(TypedDict):
    status: str
    fact_refs: list[str]
    source_refs: list[str]
    years: list[int]
    missing_metrics: list[str]
    stale_metrics: list[str]
    limitations: list[str]


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value, required=False):
    return (
        isinstance(value, list)
        and (not required or bool(value))
        and all(_text(v) for v in value)
    )


def _refs(value, allowed, required=False):
    return (
        _strings(value, required)
        and len(value) == len(set(value))
        and set(value) <= allowed
    )


def _date(value):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _problem(code, field="", message=""):
    return issue(code, field, message, stage="research_contract")


def validate_decision(decision, packet, estimate_ids=()) -> list[dict]:
    """Validate a proposed action, without treating screening cutoffs as gates."""
    if not isinstance(decision, dict):
        return [_problem("research_decision_not_object")]
    errors = []
    if set(decision) - set(ResearchDecision.__annotations__):
        errors.append(_problem("unexpected_research_decision_fields"))
    if not isinstance(decision.get("action"), str) or decision["action"] not in ACTIONS:
        errors.append(_problem("invalid_research_action"))
    action = decision.get("action") if isinstance(decision.get("action"), str) else ""
    for key in ("reason", "comparison_reason"):
        if not _text(decision.get(key)):
            errors.append(_problem("research_decision_reason_missing", key))
    facts, sources = _references(packet)
    for key, allowed in (
        ("fact_refs", facts),
        ("source_refs", sources),
        ("estimate_refs", set(estimate_ids)),
    ):
        if not _refs(decision.get(key), allowed):
            errors.append(_problem("research_decision_reference_invalid", key))
    if action in {
        "candidate",
        "observe",
        "abandon",
    } and not decision.get("source_refs"):
        errors.append(_problem("research_decision_evidence_required"))
    for key in ("next_steps", "revisit_triggers"):
        if not _strings(decision.get(key), required=key == "revisit_triggers"):
            errors.append(_problem("research_decision_followup_missing", key))
    if action in {
        "fetch",
        "research",
        "estimate",
        "narrow",
    } and not decision.get("next_steps"):
        errors.append(_problem("research_decision_next_action_required"))
    exceptions = decision.get("policy_exceptions")
    if not isinstance(exceptions, list) or any(
        not isinstance(row, dict)
        or set(row) != {"condition", "reason"}
        or not all(_text(row.get(key)) for key in ("condition", "reason"))
        for row in exceptions
    ):
        errors.append(_problem("invalid_policy_exceptions"))
    requests = decision.get("evidence_requests", [])
    if not isinstance(requests, list):
        errors.append(_problem("evidence_requests_not_list"))
    else:
        seen = set()
        for index, request in enumerate(requests):
            label = f"evidence_requests.{index}"
            if not isinstance(request, dict) or set(request) != {
                "metric",
                "period_end",
                "reason",
            }:
                errors.append(_problem("evidence_request_shape_invalid", label))
                continue
            metric, period = request["metric"], request["period_end"]
            if not isinstance(metric, str) or metric not in EVIDENCE_METRICS:
                errors.append(_problem("evidence_request_metric_invalid", label))
            if (
                not isinstance(period, str)
                or not _date(period)
                or _date(period).isoformat() != period
            ):
                errors.append(_problem("evidence_request_period_invalid", label))
            if not _text(request["reason"]):
                errors.append(_problem("evidence_request_reason_missing", label))
            if isinstance(metric, str) and isinstance(period, str):
                key = (metric, period)
                if key in seen:
                    errors.append(_problem("duplicate_evidence_request", label))
                seen.add(key)
        if action == "fetch" and not requests:
            errors.append(_problem("fetch_evidence_request_required"))
    return errors


def validate_estimates(estimates, packet) -> list[dict]:
    """Estimates are proposals, never replacements for disclosed facts."""
    if not isinstance(estimates, list):
        return [_problem("estimates_not_list")]
    errors, seen = [], set()
    facts, sources = _references(packet)
    for index, row in enumerate(estimates):
        field = str(index)
        if not isinstance(row, dict):
            errors.append(_problem("estimate_not_object", field))
            continue
        identifier = row.get("id")
        if (
            not _text(identifier)
            or identifier in seen
            or identifier in facts
            or identifier in sources
        ):
            errors.append(_problem("estimate_id_invalid_or_collides", field))
        else:
            seen.add(identifier)
            field = identifier
        if set(row) - set(Estimate.__annotations__):
            errors.append(_problem("unexpected_estimate_fields", field))
        metric = row.get("metric")
        if not isinstance(metric, str) or metric not in ESTIMATE_UNITS:
            errors.append(_problem("unsupported_estimate_metric", field))
        elif row.get("unit") != ESTIMATE_UNITS[metric]:
            errors.append(_problem("estimate_unit_mismatch", field))
        currency = row.get("currency")
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isupper()
        ):
            errors.append(_problem("estimate_currency_invalid", field))
        bounds = [row.get(key) for key in ("low", "base", "high")]
        if (
            not all(finite(v) for v in bounds)
            or not bounds[0] <= bounds[1] <= bounds[2]
        ):
            errors.append(_problem("estimate_bounds_invalid", field))
        if not _date(row.get("period_end")):
            errors.append(_problem("estimate_period_missing", field))
        if not _text(row.get("method")):
            errors.append(_problem("estimate_method_missing", field))
        for key, allowed in (("fact_refs", facts), ("source_refs", sources)):
            if not _refs(row.get(key), allowed, required=True):
                errors.append(_problem("estimate_reference_invalid", f"{field}.{key}"))
        for key in ("counterevidence", "sensitivity", "invalidation"):
            if not _strings(row.get(key), required=key != "counterevidence"):
                errors.append(_problem("estimate_limit_missing", f"{field}.{key}"))
    return errors


def assess_coverage(packet: dict, as_of: date | str) -> dict:
    """Describe usable history per conclusion; do not certify packet validity.

    Caller must first call validate_packet. A missing old PDF cannot invalidate
    unrelated evidenced cells. Recent anchors are 550-day fiscal observations;
    this is a visible recency flag, not a seven-year minimum or calendar assumption.
    """
    as_of = _date(as_of)
    if as_of is None:
        raise ValueError("as_of must be an ISO date")
    template = packet.get("company", {}).get("template", "ordinary")
    sources = {
        s["id"]: s
        for s in packet.get("sources", [])
        if isinstance(s, dict) and _text(s.get("id"))
    }
    rows = [
        f
        for f in packet.get("facts", [])
        if isinstance(f, dict) and finite(f.get("value")) and _date(f.get("period_end"))
    ]
    primary = {
        sid
        for sid, source in sources.items()
        if (source.get("document_kind") or source.get("kind"))
        in {"annual", "interim", "prospectus"}
        and source.get("verification_level") != "provider_parsed"
        and source.get("page", 0) > 0
        and source.get("text")
        and source.get("hash")
        and source.get("url")
        and source.get("origin_id")
    }
    groups = {
        "normalized_earnings": [("adjusted_net_income_parent", "net_income_parent")],
        "capital_efficiency": [("parent_equity",)],
        "normalized_cashflow": [("operating_cash_flow",), ("capital_expenditures",)],
        "implied_return": [("market_cap",)],
        "peer_thesis": [],
        "thesis_breaker": [],
    }
    if template == "reit":
        groups.update(
            normalized_earnings=[("affo",)],
            normalized_cashflow=[("affo",), ("distributable_amount",)],
            capital_efficiency=[("nav",)],
        )
    coverage = {}
    for key, requirements in groups.items():
        relevant = [
            f
            for f in rows
            if any(f.get("metric") in alternatives for alternatives in requirements)
        ]
        missing, stale, refs, source_refs = [], [], [], []
        for alternatives in requirements:
            candidates = [
                f
                for f in relevant
                if f.get("metric") in alternatives
                and (key == "implied_return" or f.get("source_id") in primary)
            ]
            if not candidates:
                missing.append("|".join(alternatives))
                continue
            newest = max(_date(f["period_end"]) for f in candidates)
            age_limit = 7 if key == "implied_return" else 550
            if (as_of - newest).days > age_limit or newest > as_of:
                stale.append("|".join(alternatives))
            refs.extend(f["id"] for f in candidates)
            source_refs.extend(f["source_id"] for f in candidates)
        years = sorted(
            {
                _date(f["period_end"]).year
                for f in relevant
                if f.get("period_type") == "annual"
            }
        )
        limits = []
        if years and len(years) < 7:
            limits.append(
                f"Only {len(years)} disclosed annual observations; compute growth over actual intervals."
            )
        if template == "cyclical" and years and len(years) < 10:
            limits.append(
                "Cycle coverage must be justified; observed years do not establish a mid-cycle baseline."
            )
        if (
            key == "normalized_earnings"
            and not any(
                f.get("metric") == "adjusted_net_income_parent" for f in relevant
            )
            and template != "reit"
        ):
            limits.append(
                "Reported earnings require an explicit normalization assessment."
            )
        if key == "capital_efficiency":
            limits.append(
                "Equity is an anchor, not proof of ROIC; leverage and capital definitions require review."
            )
        if key == "peer_thesis":
            limits.append(
                "Compare only evidenced peer metrics; no four-peer completeness gate."
            )
        if key == "thesis_breaker":
            limits.append(
                "Persistence and causal claims require document evidence and falsifiable breakers."
            )
        status = "insufficient" if missing or stale else "available"
        if key in {"peer_thesis", "thesis_breaker"}:
            status = "requires_judgment"
        if template in {"bank", "insurance"} and key == "normalized_cashflow":
            status, missing, stale = "not_applicable", [], []
            limits.append(
                "Use sector capital adequacy and payout constraints instead of corporate FCF."
            )
        coverage[key] = {
            "status": status,
            "fact_refs": sorted(set(refs)),
            "source_refs": sorted(set(source_refs)),
            "years": years,
            "missing_metrics": missing,
            "stale_metrics": stale,
            "limitations": limits,
        }
    return {
        "version": VERSION,
        "as_of": as_of.isoformat(),
        "template": template,
        "coverage": coverage,
    }


def reference_conditions(packet: dict, metrics: dict, as_of: date | str) -> dict:
    """Nonbinding conditions which a proposed candidate must explicitly address."""
    from .calculations import screen

    as_of = _date(as_of)
    if as_of is None:
        raise ValueError("as_of must be an ISO date")
    checks = {
        f"screen.{key}": value
        for key, value in screen(packet, as_of, {})["checks"].items()
    }
    template = packet["company"]["template"]
    returns = ["affo_yield"] if template == "reit" else ["implied_ke_pb"]
    if template in {"ordinary", "cyclical"}:
        returns.append("fcf_yield")
    for name in returns:
        metric = metrics.get(name, {})
        value = metric.get("value") if isinstance(metric, dict) else None
        checks[f"return.{name}"] = value >= 0.07 if finite(value) else None
    coverage = assess_coverage(packet, as_of)["coverage"]
    required_years = 10 if template == "cyclical" else 7
    history_keys = ["normalized_earnings"]
    if template in {"ordinary", "cyclical"}:
        history_keys.append("normalized_cashflow")
    checks["coverage.history"] = all(
        len(coverage[key]["years"]) >= required_years for key in history_keys
    )
    peers = packet.get("peer_packets", [])
    peer_ids = (
        {
            p.get("company", {}).get("id")
            for p in peers
            if isinstance(p, dict)
            and isinstance(p.get("company"), dict)
            and _text(p["company"].get("id"))
            and p.get("facts")
        }
        if isinstance(peers, list)
        else set()
    )
    peer_ids.discard(packet["company"]["id"])
    checks["coverage.peers"] = len(peer_ids) >= 4
    return checks


def coverage_with_estimates(packet: dict, estimates: list, as_of: date | str) -> dict:
    """Keep raw gaps while allowing a reviewed bounded shareholder cash estimate.

    The note-text check only establishes that cash-bridge material was supplied;
    it cannot establish its interpretation. Independent crosscheck must still
    review every estimate, its assumptions and the retained missing observations.
    Market value and book equity cannot be replaced through this path.
    """
    result = assess_coverage(packet, as_of)
    result["raw_coverage"] = copy.deepcopy(result["coverage"])
    errors = validate_estimates(estimates, packet)
    if errors:
        result["estimate_errors"] = errors
        return result
    if packet.get("company", {}).get("template") not in {"ordinary", "cyclical"}:
        return result
    cash = result["coverage"]["normalized_cashflow"]
    if cash["status"] != "insufficient":
        return result
    as_of = _date(as_of)
    sources = {
        s["id"]: s
        for s in packet.get("sources", [])
        if isinstance(s, dict) and _text(s.get("id"))
    }
    facts = {
        f["id"]: f
        for f in packet.get("facts", [])
        if isinstance(f, dict) and _text(f.get("id"))
    }

    def recent_primary(source):
        end = _date(source.get("period_end"))
        return (
            end is not None
            and 0 <= (as_of - end).days <= 550
            and (source.get("document_kind") or source.get("kind"))
            in {"annual", "interim", "prospectus"}
            and source.get("verification_level") != "provider_parsed"
            and finite(source.get("page"))
            and source["page"] > 0
            and source.get("text")
            and source.get("hash")
            and source.get("url")
            and source.get("origin_id")
        )

    bridge_terms = re.compile(
        r"capital[\s_]*(?:expenditure|expenditures)|capex|购建固定资产|资本(?:性)?支出|资本开支"
        r"|depreciation|折旧|摊销|maintenance[\s_]*acquisition|维持性并购"
        r"|lease[\s_]*principal|租赁(?:负债)?本金|share[ -]based[ -]compensation|股份支付|股权激励",
        re.IGNORECASE,
    )
    for estimate in estimates:
        if estimate["metric"] != "normalized_fcf":
            continue
        anchor_ids = []
        for fid in estimate["fact_refs"]:
            fact = facts.get(fid, {})
            end = _date(fact.get("period_end"))
            source_id = fact.get("source_id")
            if (
                fact.get("metric")
                in {
                    "operating_cash_flow",
                    "net_income_parent",
                    "adjusted_net_income_parent",
                }
                and finite(fact.get("value"))
                and end is not None
                and 0 <= (as_of - end).days <= 550
                and source_id in estimate["source_refs"]
                and recent_primary(sources.get(source_id, {}))
            ):
                anchor_ids.append(fid)
        bridge_ids = [
            sid
            for sid in estimate["source_refs"]
            if recent_primary(sources.get(sid, {}))
            and bridge_terms.search(str(sources[sid].get("text", "")))
        ]
        if not anchor_ids or not bridge_ids:
            continue
        cash.update(
            status="estimate_supported",
            estimate_refs=[estimate["id"]],
            support_basis={
                "primary_anchor_fact_refs": anchor_ids,
                "cash_bridge_source_refs": bridge_ids,
            },
        )
        cash["limitations"].append(
            "Missing raw cash observations remain unresolved; shareholder FCF is an explicit assumption range supported by cited financial anchors and cash-bridge material, subject to independent review."
        )
        break
    return result


def _review_errors(review, packet, estimate_ids, template, field):
    if not isinstance(review, dict):
        return [_problem("independent_review_missing", field)]
    checks = review.get("checks")
    if (
        not isinstance(checks, list)
        or len(checks) != len(REVIEW_TOPICS)
        or any(
            not isinstance(c, dict) or not isinstance(c.get("topic"), str)
            for c in checks
        )
        or {c["topic"] for c in checks} != REVIEW_TOPICS
    ):
        return [_problem("independent_review_incomplete", field)]
    errors = []
    facts, sources = _references(packet)
    for check in checks:
        label = f"{field}.{check['topic']}"
        status = check.get("status")
        na_allowed = (
            check["topic"] == "cashflow" and template in {"bank", "insurance"}
        ) or (check["topic"] == "adjustments" and not estimate_ids)
        if (
            not isinstance(status, str)
            or status not in {"supported", "limitations", "not_applicable"}
            or (status == "not_applicable" and not na_allowed)
        ):
            errors.append(_problem("review_unresolved", label))
        if not _text(check.get("reason")):
            errors.append(_problem("review_reason_missing", label))
        for key, allowed in (
            ("fact_refs", facts),
            ("source_refs", sources),
            ("estimate_refs", estimate_ids),
        ):
            if not _refs(
                check.get(key, []),
                allowed,
                required=key == "source_refs" and status != "not_applicable",
            ):
                errors.append(_problem("review_reference_invalid", f"{label}.{key}"))
    reviewed_estimates = set()
    for check in checks:
        refs = check.get("estimate_refs", [])
        if isinstance(refs, list):
            reviewed_estimates.update(ref for ref in refs if isinstance(ref, str))
    if not estimate_ids <= reviewed_estimates:
        errors.append(_problem("estimate_not_independently_reviewed", field))
    issues = review.get("issues")
    if not isinstance(issues, list) or any(
        not isinstance(row, dict)
        or row.get("severity") != "limitation"
        or not _text(row.get("reason"))
        for row in issues
    ):
        errors.append(_problem("review_blocking_issues", field))
    return errors


def report_acceptance(publication: dict) -> list[dict]:
    """Return explicit reasons why a report does not count as valid acceptance.

    This is a deterministic audit, not a guarantee of semantic correctness.
    Publication requires separately validated independent model review evidence.
    """
    if isinstance(publication, dict) and publication.get("policy_version") == "raw-pdf-sessions/1":
        from .session_review import accept_publication

        return accept_publication(publication)
    if not isinstance(publication, dict):
        return [_problem("publication_not_object")]
    packet = publication.get("evidence_packet")
    if not isinstance(packet, dict):
        return [_problem("frozen_evidence_packet_required")]
    as_of = _date(publication.get("as_of"))
    if as_of is None:
        return [_problem("publication_date_invalid")]
    accepted, errors = validate_packet(packet, as_of)
    errors = list(errors)
    company = packet.get("company", {})
    if not isinstance(company, dict):
        return errors + [_problem("publication_company_mismatch")]
    template = company.get("template")
    published_company = publication.get("company")
    if (
        not isinstance(template, str)
        or template not in TEMPLATES
        or not isinstance(published_company, dict)
        or published_company.get("id") != company.get("id")
    ):
        return errors + [_problem("publication_company_mismatch")]
    for key in ("input_hash", "policy_version"):
        if not _text(publication.get(key)):
            errors.append(_problem("publication_version_missing", key))
    try:
        if publication.get("input_hash") != fingerprint(packet):
            errors.append(_problem("publication_evidence_hash_mismatch"))
    except (TypeError, ValueError):
        errors.append(_problem("publication_evidence_not_serializable"))
    if publication.get("deterministic_errors"):
        errors.append(_problem("deterministic_verification_failed"))
    estimates = publication.get("estimates", [])
    estimate_errors = validate_estimates(estimates, packet)
    errors += estimate_errors
    estimate_ids = (
        {e["id"] for e in estimates if isinstance(e, dict) and _text(e.get("id"))}
        if isinstance(estimates, list)
        else set()
    )
    errors += validate_decision(publication.get("decision"), packet, estimate_ids)
    proposed_decision = publication.get("decision")
    if isinstance(proposed_decision, dict) and proposed_decision.get("action") in (
        "fetch",
        "defer",
    ):
        errors.append(_problem("unfinished_research_cannot_count_as_report"))
    frozen = {**packet, "facts": accepted}
    coverage = coverage_with_estimates(frozen, estimates, as_of)["coverage"]
    anchors = {"normalized_earnings", "capital_efficiency", "implied_return"}
    core = set(anchors)
    if template in {"ordinary", "cyclical", "reit"}:
        anchors.add("normalized_cashflow")
        core = {"normalized_earnings", "normalized_cashflow", "implied_return"}
    for key in anchors:
        if coverage[key]["status"] == "insufficient":
            errors.append(_problem("recent_primary_anchor_missing", key))
    metrics = publication.get("metrics", {})
    if not isinstance(metrics, dict):
        return errors + [_problem("publication_metrics_invalid")]
    if not estimate_errors and len(accepted) == len(packet.get("facts", [])):
        # Late import keeps evidence contracts usable by the calculator itself.
        from .research_calculations import calculate_research

        recalculated, calculation_errors = calculate_research(frozen, estimates)
        errors += calculation_errors
        # JSON storage stringifies fiscal-year dictionary keys. Compare the
        # actual wire representation so a valid report survives persistence.
        try:
            matches = canonical(recalculated) == canonical(metrics)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            errors.append(_problem("publication_calculation_mismatch"))
    facts, sources = _references(packet)
    valid_metrics = set()
    for name, metric in metrics.items():
        if not isinstance(metric, dict) or metric.get("value") is None:
            continue
        if metric.get("diagnostic_only") is True:
            continue
        bounds = metric.get("range", {})
        if (
            not isinstance(bounds, dict)
            or not all(finite(bounds.get(k)) for k in ("low", "base", "high"))
            or not bounds["low"] <= bounds["base"] <= bounds["high"]
            or not finite(metric.get("value"))
            or metric["value"] != bounds["base"]
        ):
            errors.append(_problem("calculation_range_invalid", name))
            continue
        if (
            not _text(metric.get("formula"))
            or not _refs(metric.get("fact_refs", []), facts)
            or not _refs(metric.get("estimate_refs", []), estimate_ids)
            or not (metric.get("fact_refs") or metric.get("estimate_refs"))
        ):
            errors.append(_problem("calculation_evidence_invalid", name))
            continue
        valid_metrics.add(name)
    conclusions = publication.get("conclusions")
    if (
        not isinstance(conclusions, list)
        or len(conclusions) != 6
        or any(
            not isinstance(c, dict) or not isinstance(c.get("key"), str)
            for c in conclusions
        )
        or {c["key"] for c in conclusions} != CONCLUSION_KEYS
    ):
        errors.append(_problem("six_conclusions_required"))
    else:
        for claim in conclusions:
            key = claim["key"]
            status = claim.get("status")
            if not isinstance(status, str) or status not in CONCLUSION_STATUSES:
                errors.append(_problem("invalid_claim_status", key))
            substantive = isinstance(status, str) and status in {"supported", "refuted"}
            if key in core and not substantive:
                errors.append(_problem("material_conclusion_required", key))
            if not _text(claim.get("reason")):
                errors.append(_problem("claim_reason_missing", key))
            for field in ("counterevidence", "assumptions", "invalidation"):
                if not _strings(claim.get(field), required=field == "invalidation"):
                    errors.append(_problem("claim_limit_missing", f"{key}.{field}"))
            for field, allowed in (
                ("fact_refs", facts),
                ("source_refs", sources),
                ("estimate_refs", estimate_ids),
                ("calculation_refs", valid_metrics),
            ):
                if not _refs(
                    claim.get(field, []),
                    allowed,
                    required=(field == "source_refs" and substantive)
                    or (field == "calculation_refs" and key in core),
                ):
                    errors.append(_problem("claim_reference_invalid", f"{key}.{field}"))
            calculation_refs = claim.get("calculation_refs", [])
            if key in core and (
                not _strings(calculation_refs)
                or not set(calculation_refs) & CORE_METRICS[key] & valid_metrics
            ):
                errors.append(_problem("material_calculation_required", key))
    review = publication.get("review", {})
    if not isinstance(review, dict):
        review = {}
    if review.get("independence") != INDEPENDENCE:
        errors.append(_problem("review_context_independence_missing"))
    for name in ("blind", "crosscheck"):
        errors += _review_errors(
            review.get(name),
            packet,
            estimate_ids if name == "crosscheck" else set(),
            template,
            name,
        )
    decision = publication.get("decision")
    exceptions = (
        decision.get("policy_exceptions", []) if isinstance(decision, dict) else []
    )
    conditions = (
        {
            row["condition"]
            for row in exceptions
            if isinstance(row, dict) and _text(row.get("condition"))
        }
        if isinstance(exceptions, list)
        else set()
    )
    if isinstance(decision, dict) and decision.get("action") == "candidate":
        required_exceptions = {
            key
            for key, passed in reference_conditions(frozen, metrics, as_of).items()
            if passed is not True
        }
        for condition in sorted(required_exceptions - conditions):
            errors.append(_problem("candidate_policy_exception_required", condition))
    crosscheck = review.get("crosscheck")
    reviewed = (
        crosscheck.get("policy_exceptions", []) if isinstance(crosscheck, dict) else []
    )
    if not isinstance(reviewed, list) or any(
        not isinstance(row, dict)
        or not _text(row.get("condition"))
        or not isinstance(row.get("status"), str)
        or row.get("status") not in {"supported", "limitations"}
        or not _text(row.get("reason"))
        or not _refs(row.get("source_refs"), sources, required=True)
        for row in reviewed
    ):
        errors.append(_problem("policy_exception_review_invalid"))
    elif {row["condition"] for row in reviewed} != conditions or len(reviewed) != len(
        conditions
    ):
        errors.append(_problem("policy_exception_not_reviewed"))
    usage = publication.get("usage")
    if not isinstance(usage, dict) or any(
        not isinstance(usage.get(k), int) or isinstance(usage[k], bool) or usage[k] < 0
        for k in ("model_calls", "input_tokens", "output_tokens")
    ):
        errors.append(_problem("actual_usage_required"))
    return errors
