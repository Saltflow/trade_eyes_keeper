"""Evidence-bound accounting treatments, separate from immutable raw facts."""

from __future__ import annotations

import math
import re
from datetime import date

from .contracts import finite, issue

COMPONENTS = (
    "interest_not_in_cfo",
    "lease_principal",
    "sbc_cost",
    "maintenance_acquisitions",
)
OBSERVATIONS = {
    "interest_not_in_cfo": {"interest_not_in_cfo", "interest_paid"},
    "lease_principal": {"lease_principal", "lease_payments"},
    "sbc_cost": {"sbc_cost", "share_based_compensation"},
    "maintenance_acquisitions": {"maintenance_acquisitions", "acquisitions_cash"},
}
TERMS = {
    "interest_not_in_cfo": r"利息|interest",
    "lease_principal": r"租赁|lease",
    "sbc_cost": r"股份支付|股权激励|share.based|stock.based",
    "maintenance_acquisitions": r"收购|并购|合并|acquisition|business combination",
}


def requirements(packet, settings=None):
    """Choose the recent 3–5 actual CFO/Capex years; never backfill a gap."""
    settings = settings or {}
    width = settings.get("normalized_fcf_years", 5)
    if type(width) is not int or not 3 <= width <= 5:
        raise ValueError("normalized_fcf_years must be an integer from 3 to 5")
    annual = [f for f in packet.get("facts", []) if f.get("period_type") == "annual"]
    earnings = [
        int(f["period_end"][:4])
        for f in annual
        if f["metric"] == "adjusted_net_income_parent"
    ]
    end = max(earnings, default=0)
    available = {
        metric: {int(f["period_end"][:4]) for f in annual if f["metric"] == metric}
        for metric in ("operating_cash_flow", "capital_expenditures")
    }
    common = available["operating_cash_flow"] & available["capital_expenditures"]
    count = 0
    while count < width and end - count in common:
        count += 1
    return [
        (name, year)
        for year in range(end - max(3, count) + 1, end + 1)
        for name in COMPONENTS
    ]


def explicit_entries(packet, settings=None):
    """Printed component observations need no model-created accounting fact."""
    wanted = set(requirements(packet, settings))
    primary = {
        s["id"]
        for s in packet.get("sources", [])
        if s.get("kind") in {"annual", "interim"}
        and s.get("url")
        and s.get("page", 0) > 0
        and s.get("verification_level") != "provider_parsed"
        and s.get("document_status") != "incomplete_text"
    }
    rows = {}
    for fact in packet.get("facts", []):
        key = (fact.get("metric"), int(fact["period_end"][:4]))
        if key not in wanted or fact.get("period_type") != "annual":
            continue
        if fact.get("source_id") not in primary:
            continue
        if not finite(fact.get("value")) or fact["value"] < 0:
            continue
        rows[key] = {
            "id": "disclosed:" + fact["id"],
            "component": key[0],
            "period_end": fact["period_end"],
            "amount": fact["value"],
            "currency": fact["currency"],
            "unit": fact["unit"],
            "treatment": "deduct" if fact["value"] else "not_applicable",
            "fact_refs": [fact["id"]],
            "source_refs": [fact["source_id"]],
            "reason": "Explicit reported component; subject to full independent evidence review",
            "origin": "reported_fact",
        }
    return rows


def missing_requirements(packet, settings=None):
    observed = explicit_entries(packet, settings)
    return [
        {"component": name, "year": year}
        for name, year in requirements(packet, settings)
        if (name, year) not in observed
    ]


def validate_proposal(packet, proposal, settings=None):
    errors = []
    wanted = {
        (r["component"], r["year"]) for r in missing_requirements(packet, settings)
    }
    entries = proposal.get("entries") if isinstance(proposal, dict) else None
    if (
        not isinstance(entries, list)
        or proposal.get("issues") != []
        or set(proposal) != {"entries", "issues"}
    ):
        return [issue("cashflow_bridge_proposal_incomplete", stage="bridge")]
    sources = {s["id"]: s for s in packet.get("sources", [])}
    facts = {f["id"]: f for f in packet.get("facts", [])}
    seen, ids, deducted = set(), set(), set()
    for entry in entries:
        try:
            if not isinstance(entry, dict) or set(entry) != {
                "id",
                "component",
                "period_end",
                "amount",
                "unit",
                "currency",
                "treatment",
                "fact_refs",
                "source_refs",
                "quotes",
                "reason",
            }:
                raise ValueError("invalid bridge entry schema")
            name, period, amount = (
                entry["component"],
                entry["period_end"],
                entry["amount"],
            )
            period_date = date.fromisoformat(period)
            key = (name, int(period[:4]))
            if (
                key not in wanted
                or key in seen
                or not entry["id"]
                or entry["id"] in ids
            ):
                raise ValueError("unexpected or duplicate bridge cell")
            seen.add(key)
            ids.add(entry["id"])
            if not finite(amount) or amount < 0 or entry.get("unit") != "currency":
                raise ValueError("invalid accounting amount or unit")
            if (
                not isinstance(entry.get("reason"), str)
                or not entry["reason"].strip()
                or not isinstance(entry.get("currency"), str)
            ):
                raise ValueError("missing accounting rationale or currency")
            if not any(
                f.get("metric") == "operating_cash_flow"
                and f.get("period_type") == "annual"
                and f["period_end"] == period
                and f["currency"] == entry["currency"]
                and f["unit"] == entry["unit"]
                for f in facts.values()
            ):
                raise ValueError("bridge period and currency do not match annual CFO")
            refs = entry.get("source_refs", [])
            if (
                not isinstance(refs, list)
                or not refs
                or any(not isinstance(ref, str) or ref not in sources for ref in refs)
            ):
                raise ValueError("missing original source")
            originals = [sources[ref] for ref in refs]
            if any(
                s.get("kind") not in {"annual", "interim"}
                or not s.get("url")
                or s.get("page", 0) <= 0
                or s.get("verification_level") == "provider_parsed"
                or s.get("document_status") == "incomplete_text"
                for s in originals
            ):
                raise ValueError("bridge requires complete primary originals")
            if any(
                date.fromisoformat(s["period_end"]) < period_date
                or date.fromisoformat(s["published_at"]) < period_date
                for s in originals
            ):
                raise ValueError(
                    "original disclosure cannot support this fiscal period"
                )
            quotes = entry.get("quotes", [])
            if (
                not isinstance(quotes, list)
                or not quotes
                or any(
                    not isinstance(q, dict)
                    or q.get("source_id") not in refs
                    or not q.get("text")
                    or not isinstance(q["text"], str)
                    or q["text"] not in sources[q["source_id"]].get("text", "")
                    for q in quotes
                )
            ):
                raise ValueError("unsupported bridge quotation")
            text = " ".join(q["text"] for q in quotes)
            if not re.search(TERMS[name], text, re.IGNORECASE):
                raise ValueError("quotation does not identify the component")
            fact_refs = entry.get("fact_refs", [])
            if not isinstance(fact_refs, list) or any(
                not isinstance(ref, str) or ref not in facts for ref in fact_refs
            ):
                raise ValueError("unknown accounting fact")
            observed = [facts[ref] for ref in fact_refs]
            if any(
                f["period_end"] != period
                or f.get("period_type") != "annual"
                or f["currency"] != entry["currency"]
                or f["unit"] != entry["unit"]
                or f["source_id"] not in refs
                for f in observed
            ):
                raise ValueError("fact period, unit, currency or provenance mismatch")
            suitable = [f for f in observed if f["metric"] in OBSERVATIONS[name]]
            treatment = entry.get("treatment")
            if treatment == "deduct":
                matching = [
                    f
                    for f in suitable
                    if math.isclose(
                        amount,
                        f["value"],
                        rel_tol=0,
                        abs_tol=abs(f.get("precision", 0) * f.get("multiplier", 1)) / 2,
                    )
                ]
                if amount <= 0 or not matching:
                    raise ValueError(
                        "deduction must equal a disclosed component amount"
                    )
                magnitude_id = matching[0]["id"]
                if magnitude_id in deducted:
                    raise ValueError("same expenditure deducted twice")
                deducted.add(magnitude_id)
            elif treatment == "already_in_cfo":
                if (
                    amount != 0
                    or not suitable
                    or not re.search(
                        r"经营活动|operating (?:activities|cash flow)",
                        text,
                        re.IGNORECASE,
                    )
                ):
                    raise ValueError(
                        "already-in-CFO requires amount and explicit classification"
                    )
            elif treatment == "not_applicable":
                zero_fact = any(f["value"] == 0 for f in suitable)
                absence = re.search(
                    r"不适用|未发生|无此|无.{0,12}(?:收购|租赁|股份支付)|no (?:such )?(?:acquisitions|leases|share.based|stock.based|interest)|not applicable",
                    text,
                    re.IGNORECASE,
                )
                if re.search(
                    r"(?:无|未发生).{0,3}(?:重大|重要)|no\s+(?:material|significant)",
                    text,
                    re.IGNORECASE,
                ):
                    absence = None
                if amount != 0 or not (zero_fact or absence):
                    raise ValueError("zero requires explicit nil disclosure")
            else:
                raise ValueError("unsupported accounting treatment")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                issue(
                    "cashflow_bridge_invalid",
                    str(entry.get("id", "")) if isinstance(entry, dict) else "",
                    str(exc),
                    stage="bridge",
                )
            )
    if seen != wanted:
        errors.append(
            issue(
                "cashflow_bridge_missing",
                message=str(sorted(wanted - seen)),
                stage="bridge",
            )
        )
    return errors


def validate_bridge_review(proposal, review):
    entries = {e["id"]: e for e in proposal.get("entries", [])}
    checks = review.get("checks") if isinstance(review, dict) else None
    if (
        not isinstance(checks, list)
        or review.get("issues") != []
        or set(review) != {"checks", "issues"}
    ):
        return [issue("cashflow_bridge_review_incomplete", stage="review")]
    errors, seen = [], set()
    for check in checks:
        ident = check.get("bridge_id") if isinstance(check, dict) else None
        entry = entries.get(ident) if isinstance(ident, str) else None
        if (
            not entry
            or ident in seen
            or check.get("status") != "supported"
            or not check.get("reason")
        ):
            errors.append(
                issue(
                    "cashflow_bridge_review_not_supported", str(ident), stage="review"
                )
            )
            continue
        seen.add(ident)
        if (
            not isinstance(check.get("source_refs"), list)
            or not isinstance(check.get("fact_refs"), list)
            or any(
                not isinstance(ref, str)
                for ref in [*check["source_refs"], *check["fact_refs"]]
            )
            or set(entry["source_refs"]) != set(check["source_refs"])
            or set(entry.get("fact_refs", [])) != set(check["fact_refs"])
        ):
            errors.append(
                issue("cashflow_bridge_review_reference_invalid", ident, stage="review")
            )
    if seen != set(entries):
        errors.append(issue("cashflow_bridge_review_incomplete", stage="review"))
    return errors


def resolve_bridge(packet, settings=None):
    rows = explicit_entries(packet, settings)
    if not missing_requirements(packet, settings):
        return rows, []
    bridge = packet.get("cashflow_bridge") or {}
    if not isinstance(bridge, dict):
        return rows, [issue("cashflow_bridge_proposal_incomplete", stage="bridge")]
    proposal = bridge.get("proposal", {})
    errors = validate_proposal(packet, proposal, settings)
    if not errors:
        errors += validate_bridge_review(proposal, bridge.get("review", {}))
    if errors:
        return rows, errors
    for entry in proposal["entries"]:
        rows[(entry["component"], int(entry["period_end"][:4]))] = entry
    return rows, []
