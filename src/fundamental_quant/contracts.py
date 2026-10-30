"""Small, deterministic contracts at every untrusted-data boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from typing import Any

from .currency_definitions import (
    currency_definition_supports_unit,
    statement_currency_supports_fact,
)

CONCLUSION_KEYS = {
    "normalized_earnings",
    "capital_efficiency",
    "normalized_cashflow",
    "implied_return",
    "peer_thesis",
    "thesis_breaker",
}
CONCLUSION_STATUSES = {
    "supported",
    "refuted",
    "insufficient",
    "conflict",
    "not_applicable",
}
TEMPLATES = {"ordinary", "cyclical", "bank", "insurance", "reit"}
RATIO_METRICS = {
    "pe_ttm",
    "adjusted_pe_ttm",
    "pb",
    "ev_ebit",
    "dividend_yield",
    "reported_roe",
    "adjusted_roe",
    "roe",
    "roic",
    "net_interest_margin",
    "credit_cost",
    "capital_adequacy",
    "nonperforming_loan_ratio",
    "provision_coverage",
    "payout_ratio",
    "investment_yield",
    "solvency_ratio",
    "combined_ratio",
    "interest_coverage",
    "leverage",
    "financing_cost",
    "occupancy_rate",
    "rent_growth",
    "net_margin",
    "operating_margin",
    "reinvestment_rate",
    "retention_rate",
    "fcf_conversion",
}
PER_SHARE_METRICS = {"price", "basic_eps", "diluted_eps", "dpu", "nav_per_share"}
UNITS = {"currency", "ratio", "per_share", "shares", "years"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def issue(
    code: str, field: str = "", message: str = "", *, stage="data", company=""
) -> dict:
    return {
        "code": code,
        "stage": stage,
        "field": field,
        "message": message or code,
        "company": company,
        "affected_conclusions": sorted(CONCLUSION_KEYS),
        "retry_condition": "relevant_evidence_or_contract_changes",
    }


def envelope(status: str, data: Any, as_of: str, errors=(), **kwargs) -> dict:
    return {
        "status": status,
        "data": data,
        "source_refs": [],
        "as_of": as_of,
        "coverage": {},
        "errors": list(errors),
        "retryable": False,
        "next_action": "inspect_errors" if errors else "continue",
        **kwargs,
    }


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _refs(value: Any, allowed: set[str], *, required: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not required or bool(value))
        and all(isinstance(ref, str) and ref in allowed for ref in value)
        and len(value) == len(set(value))
    )


def _references(packet: dict) -> tuple[set[str], set[str]]:
    """Union only structural IDs; service verifies each peer packet separately."""
    packets = [packet]
    peers = packet.get("peer_packets", [])
    if isinstance(peers, list):
        packets.extend(p for p in peers if isinstance(p, dict))
    facts, sources = set(), set()
    for evidence in packets:
        for field, target in (("facts", facts), ("sources", sources)):
            rows = evidence.get(field, [])
            if isinstance(rows, list):
                target.update(
                    row["id"]
                    for row in rows
                    if isinstance(row, dict) and _text(row.get("id"))
                )
    return facts, sources


def _canonical_fact(fact: dict) -> dict:
    """Normalize only explicit synonymous units; never infer exchange rates."""
    result = dict(fact)
    metric, unit, currency = (
        result.get("metric"),
        result.get("unit"),
        result.get("currency"),
    )
    if isinstance(unit, str) and unit == currency:
        result["unit"] = "per_share" if metric in PER_SHARE_METRICS else "currency"
    elif (
        metric in PER_SHARE_METRICS
        and isinstance(unit, str)
        and unit in {"currency_per_share", "price"}
        or (
            isinstance(currency, str)
            and unit == currency + "_per_share"
            and metric in PER_SHARE_METRICS
        )
    ):
        result["unit"] = "per_share"
    elif unit == "money":
        result["unit"] = "currency"
    return result


def _literal_quote(text: str, quote: Any) -> str | None:
    """Restore only PDF whitespace, retaining an exact original source span.

    Never join separate numeric cells, change punctuation, reorder labels, or
    select an ambiguous occurrence. Semantic table reconstruction is not done.
    """
    if not _text(quote):
        return None
    if quote in text:
        return quote

    def compact(value):
        chars, positions = [], []
        numeric = set("0123456789.,+-−－()%％‰")
        for match in re.finditer(r"\s+|\S", value):
            token = match.group()
            if token.isspace():
                before = value[match.start() - 1] if match.start() else ""
                after = value[match.end()] if match.end() < len(value) else ""
                if before in numeric and after in numeric:
                    chars.append(" ")
                    positions.append(match.start())
            else:
                chars.append(token)
                positions.append(match.start())
        return "".join(chars), positions

    needle, _ = compact(quote)
    haystack, offsets = compact(text)
    if not needle:
        return None
    start = haystack.find(needle)
    if start < 0 or haystack.find(needle, start + 1) >= 0:
        return None
    return text[offsets[start] : offsets[start + len(needle) - 1] + 1]


def _restore_quote(fact: dict, field: str, source: dict) -> None:
    supplied = fact.get(field)
    restored = _literal_quote(source.get("text", ""), supplied)
    if restored is not None and restored != supplied:
        fact[field] = restored
        fact.setdefault("quote_repairs", []).append(
            {
                "field": field,
                "source_id": source["id"],
                "submitted": supplied,
                "original": restored,
                "method": "unique_whitespace_only_source_span",
            }
        )


def _quoted_numbers(quote: str) -> list[float]:
    """Read literal signs, including accounting parentheses and Unicode minus.

    Parenthesized tokens are consumed as a whole so '(100)' cannot also provide
    a positive 100 match. This is lexical validation, not accounting semantics.
    """
    normalized = quote.translate(
        str.maketrans({"−": "-", "－": "-", "（": "(", "）": ")"})
    )
    atom = r"[+-]?\s*\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?"
    tokens = re.findall(r"\(\s*" + atom + r"\s*\)|(?<![0-9.])" + atom, normalized)
    result = []
    for token in tokens:
        parenthesized = token.lstrip().startswith("(")
        value = float(re.sub(r"[(),\s]", "", token))
        result.append(-abs(value) if parenthesized else value)
    return result


def _unit_problem(fact: dict) -> str | None:
    metric, unit, currency = fact.get("metric"), fact.get("unit"), fact.get("currency")
    if not _text(metric):
        return "invalid_metric"
    if not isinstance(unit, str) or unit not in UNITS:
        return "invalid_unit"
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        return "invalid_currency"
    expected = (
        "ratio"
        if metric in RATIO_METRICS
        else "per_share"
        if metric in PER_SHARE_METRICS
        else "shares"
        if metric == "total_shares"
        else "years"
        if metric == "asset_life_years"
        else None
    )
    if expected is not None and unit != expected:
        return "metric_unit_mismatch"
    return None


def prepare_extracted_facts(
    packet: dict, proposals: list, model_issues: list, as_of: date
) -> tuple[list[dict], list[dict]]:
    """Validate printed units and observations before minting immutable facts.

    A proposal is not a fact. Numeric conversion and identity belong to code;
    model warnings naming a proposal quarantine it regardless of severity.
    """
    sources = {s["id"]: s for s in packet.get("sources", [])}
    if not isinstance(model_issues, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("fact_refs", []), list)
        for item in model_issues
    ):
        return [], [issue("extraction_issues_contract_invalid", stage="extract")]
    disputed = {
        ref
        for warning in model_issues
        if isinstance(warning, dict)
        for ref in warning.get("fact_refs", [])
        if isinstance(ref, str)
    }
    accepted, errors = [], []
    for index, incoming in enumerate(proposals):
        label = (
            str(incoming.get("id", index)) if isinstance(incoming, dict) else str(index)
        )
        try:
            if not isinstance(incoming, dict):
                raise TypeError("proposal_not_object")
            if incoming.get("id") in disputed:
                raise ValueError("proposal_flagged_by_extractor")
            fact = _canonical_fact(incoming)
            # Audit entries are produced by code, never trusted from a model.
            fact.pop("quote_repairs", None)
            source = sources[fact["source_id"]]
            unit_source_id = fact.get("unit_source_id") or fact["source_id"]
            unit_source = sources[unit_source_id]
            currency_source_id = fact.get("currency_source_id") or unit_source_id
            currency_source = sources[currency_source_id]
            if (
                unit_source.get("origin_id") != source.get("origin_id")
                or unit_source.get("published_at") != source.get("published_at")
                or (unit_source.get("issuer_id") != source.get("issuer_id"))
            ):
                raise ValueError("unit_header_must_belong_to_same_original_filing")
            if (
                not source.get("origin_id")
                or currency_source.get("origin_id") != source.get("origin_id")
                or currency_source.get("published_at") != source.get("published_at")
                or currency_source.get("issuer_id") != source.get("issuer_id")
            ):
                raise ValueError(
                    "currency_declaration_must_belong_to_same_original_filing"
                )
            _restore_quote(fact, "unit_quote", unit_source)
            _restore_quote(fact, "currency_quote", currency_source)
            _restore_quote(fact, "quote", source)
            unit_quote = fact.get("unit_quote")
            if not _text(unit_quote) or unit_quote not in unit_source.get("text", ""):
                raise ValueError("explicit_unit_quote_required")
            unit_text = unit_quote.lower().replace(" ", "")
            if fact.get("unit") == "ratio":
                expected_scale = (
                    0.001
                    if "‰" in unit_text
                    else 0.01
                    if re.search(r"%|％|百分|percent", unit_text)
                    else 1
                    if re.search(r"小数|decimal", unit_text)
                    else None
                )
            elif fact.get("unit") == "years":
                expected_scale = 1 if re.search(r"年|year", unit_text) else None
            else:
                expected_scale = None
                for pattern, scale in (
                    (r"万亿|trillion", 1e12),
                    (r"百亿", 1e10),
                    (r"十亿|billion", 1e9),
                    (r"亿", 1e8),
                    (r"千万", 1e7),
                    (r"百万|million", 1e6),
                    (r"万", 1e4),
                    (r"千|thousand|['’]000(?!0)", 1e3),
                    (r"百元|hundred", 100),
                    (r"人民币分|美分|港仙|cents?", 0.01),
                    (r"元|股|yuan|dollars?|shares?|rmb|cny|usd|hkd", 1),
                ):
                    if re.search(pattern, unit_text):
                        expected_scale = scale
                        break
                if fact.get("unit") in {"currency", "per_share"}:
                    currencies = {
                        "CNY": r"人民币|rmb|cny",
                        "USD": r"美元|usd|us\$",
                        "HKD": r"港元|港币|hkd|hk\$",
                        "EUR": r"欧元|eur",
                        "GBP": r"英镑|gbp",
                        "JPY": r"日元|jpy",
                    }
                    currency_quote = fact.get("currency_quote", unit_quote)
                    if (
                        not _text(currency_quote)
                        or currency_quote not in currency_source.get("text", "")
                        or not re.search(
                            currencies.get(fact.get("currency"), r"(?!)"),
                            currency_quote,
                            re.IGNORECASE,
                        )
                    ):
                        raise ValueError("currency_not_supported_by_original_unit")
                    if any(
                        code != fact.get("currency")
                        and re.search(pattern, unit_quote, re.IGNORECASE)
                        for code, pattern in currencies.items()
                    ):
                        raise ValueError(
                            "currency_declaration_conflicts_with_table_header"
                        )
                    # A distant page mentioning currency is not authority for
                    # this table: require its literal report-wide unit definition.
                    if (
                        currency_source_id != unit_source_id
                        or fact.get("currency_scope") == "financial_statements"
                    ):
                        glossary_scope = (
                            fact.get("currency_scope") == "report_unit_definition"
                            and currency_definition_supports_unit(
                                currency_quote, unit_quote
                            )
                        )
                        statement_scope = (
                            fact.get("currency_scope") == "financial_statements"
                            and statement_currency_supports_fact(
                                currency_quote,
                                unit_quote,
                                source,
                                unit_source,
                                currency_source,
                                list(sources.values()),
                                fact.get("quote", ""),
                            )
                        )
                        if fact.get("currency") != "CNY" or not (
                            glossary_scope or statement_scope
                        ):
                            raise ValueError("cross_page_currency_scope_not_explicit")
                if fact.get("unit") == "per_share" and expected_scale != 1:
                    raise ValueError("aggregate_table_scale_cannot_scale_per_share")
            raw, scale = fact.get("raw_value"), fact.get("multiplier")
            if expected_scale is None or not finite(scale) or scale != expected_scale:
                raise ValueError("multiplier_not_supported_by_original_unit")
            if not finite(raw):
                raise ValueError("printed_raw_value_required")
            quote = fact.get("quote", "")
            if (
                not _text(quote)
                or quote not in source.get("text", "")
                or not any(
                    math.isclose(raw, value, rel_tol=0, abs_tol=1e-12)
                    for value in _quoted_numbers(quote)
                )
            ):
                raise ValueError("raw_value_not_printed_in_quote")
            if fact.get("metric") == "income_tax_expense" and (
                not re.search(
                    r"所得税费用|所得税开支|income tax expense|income tax charge|tax expense",
                    quote,
                    re.IGNORECASE,
                )
                or re.search(r"递延|deferred", quote, re.IGNORECASE)
            ):
                raise ValueError("income_tax_proxy_is_not_total_expense")
            if fact.get("metric") == "cash_and_cash_equivalents" and not re.search(
                r"现金(?:及|和)现金等价物|cash and cash equivalents",
                quote,
                re.IGNORECASE,
            ):
                raise ValueError(
                    "cash_components_are_not_reported_cash_equivalents_total"
                )
            fact["value"] = raw * scale
            fact["status"] = "reported"
            fact["unit_source_id"] = unit_source_id
            fact["currency_source_id"] = currency_source_id
            fact["id"] = "extracted:" + fingerprint(
                {
                    "issuer": packet["company"]["id"],
                    "source_id": fact["source_id"],
                    "source_hash": source.get("hash"),
                    **{
                        key: fact.get(key)
                        for key in (
                            "metric",
                            "period_end",
                            "period_type",
                            "raw_value",
                            "multiplier",
                            "unit",
                            "currency",
                            "basis",
                        )
                    },
                }
            )
            subset = {
                **packet,
                "sources": list(
                    {
                        s["id"]: s for s in (source, unit_source, currency_source)
                    }.values()
                ),
                "facts": [fact],
            }
            valid, problems = validate_packet(subset, as_of)
            if problems:
                errors.extend(problems)
            else:
                accepted.extend(valid)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                issue("extraction_proposal_rejected", label, str(exc), stage="extract")
            )
    return accepted, errors


def validate_packet(packet: dict, as_of: date) -> tuple[list[dict], list[dict]]:
    """Return only numerically evidenced facts; reject each bad cell explicitly.

    Finding a number in a quote is a necessary mechanical check, not a semantic
    claim that the number belongs to the intended accounting concept.
    """
    errors, accepted = [], []
    if not isinstance(packet, dict):
        return [], [issue("packet_not_object")]
    company = packet.get("company")
    if not isinstance(company, dict):
        return [], [issue("invalid_company_contract")]
    issuer, template = company.get("id"), company.get("template")
    if not _text(issuer) or not isinstance(template, str) or template not in TEMPLATES:
        return [], [issue("invalid_company_contract")]
    if not isinstance(packet.get("sources"), list) or not isinstance(
        packet.get("facts"), list
    ):
        return [], [issue("sources_and_facts_must_be_lists", company=issuer)]
    sources = {}
    duplicate_sources = set()
    for source in packet.get("sources", []):
        if not isinstance(source, dict):
            errors.append(issue("source_not_object", company=issuer))
            continue
        sid = source.get("id")
        if not _text(sid):
            errors.append(issue("duplicate_or_missing_source_id", str(sid)))
            continue
        if sid in sources or sid in duplicate_sources:
            duplicate_sources.add(sid)
            sources.pop(sid, None)
            errors.append(issue("duplicate_or_missing_source_id", sid))
            continue
        text = source.get("text", "")
        if not isinstance(text, str) or not text.strip():
            errors.append(issue("empty_source", sid))
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if source.get("hash") != digest:
            errors.append(issue("source_hash_mismatch", sid))
            continue
        published = _date(source.get("published_at"))
        if not published or published > as_of or source.get("issuer_id") != issuer:
            errors.append(issue("source_identity_or_disclosure_mismatch", sid))
            continue
        if not _text(source.get("origin_id")):
            errors.append(issue("source_origin_missing", sid))
            continue
        sources[sid] = source
    ids, cells = set(), {}
    conflicted_cells = set()
    for incoming in packet.get("facts", []):
        if not isinstance(incoming, dict):
            errors.append(issue("fact_not_object", company=issuer))
            continue
        if not _text(incoming.get("metric")):
            errors.append(issue("fact_metric_missing", company=issuer))
            continue
        fact = _canonical_fact(incoming)
        fid = fact.get("id")
        problem = None
        source_id = fact.get("source_id")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        published, period = (
            _date(fact.get("published_at")),
            _date(fact.get("period_end")),
        )
        if not _text(fid) or fid in ids:
            problem = "duplicate_or_missing_fact_id"
            accepted = [row for row in accepted if row["id"] != fid]
        elif not finite(fact.get("value")):
            problem = "nonfinite_or_missing_value"
        elif not source:
            problem = "missing_verified_source"
        elif not published or not period or period > published or published > as_of:
            problem = "invalid_fact_date"
        elif fact.get("published_at") != source.get("published_at"):
            problem = "fact_disclosure_mismatch"
        elif source.get("kind") in ("annual", "interim") and (
            not _date(source.get("period_end"))
            or period > _date(source.get("period_end"))
        ):
            problem = "fact_after_source_fiscal_period"
        elif not isinstance(fact.get("period_type"), str) or fact[
            "period_type"
        ] not in {"annual", "interim", "ttm", "instant"}:
            problem = "missing_period_type"
        elif _unit_problem(fact):
            problem = _unit_problem(fact)
        elif (
            fact.get("period_type") == "interim"
            and type(fact.get("is_cumulative")) is not bool
        ):
            problem = "interim_cumulative_basis_required"
        elif fact.get("basis") is not None and fact.get("basis") not in (
            "average",
            "end",
            "cash_equivalents_excludes_restricted",
        ):
            problem = "invalid_capital_basis"
        elif fact.get("status") not in (
            None,
            "available",
            "verified",
            "reported",
            "derived",
        ):
            problem = "unusable_fact_status"
        else:
            quote = fact.get("quote", "")
            raw = fact.get("raw_value", fact["value"])
            scale = fact.get("multiplier", 1)
            precision = fact.get("precision", 0)
            if (
                not isinstance(quote, str)
                or not quote.strip()
                or quote not in source["text"]
            ):
                problem = "quote_not_in_source"
            elif (
                not finite(raw)
                or not finite(scale)
                or scale <= 0
                or not finite(precision)
                or not 0 <= precision <= 1
            ):
                problem = "invalid_numeric_provenance"
            elif not math.isclose(
                raw * scale, fact["value"], rel_tol=1e-12, abs_tol=1e-12
            ):
                problem = "unit_conversion_mismatch"
            else:
                numbers = _quoted_numbers(quote)
                if not any(
                    math.isclose(n, raw, rel_tol=1e-12, abs_tol=1e-12) for n in numbers
                ):
                    problem = "value_not_in_quote"
        if isinstance(fid, str):
            ids.add(fid)
        if problem:
            errors.append(issue(problem, str(fid), company=issuer))
            continue
        key = (fact.get("metric"), fact.get("period_end"), fact.get("period_type"))
        if key in conflicted_cells:
            continue
        prior = cells.get(key)
        accounting_conflict = prior and any(
            prior.get(field) != fact.get(field)
            for field in ("currency", "unit", "basis", "is_cumulative")
        )
        tolerance = (
            max(
                prior.get("precision", 0) * prior.get("multiplier", 1) if prior else 0,
                fact.get("precision", 0) * fact.get("multiplier", 1),
            )
            / 2
        )
        if prior and (
            accounting_conflict
            or not math.isclose(
                prior["value"], fact["value"], rel_tol=1e-12, abs_tol=tolerance
            )
        ):
            errors.append(
                issue("conflicting_observations", fact["metric"], company=issuer)
            )
            # Never select a convenient value from unresolved conflicting sources.
            accepted = [
                row
                for row in accepted
                if (row.get("metric"), row.get("period_end"), row.get("period_type"))
                != key
            ]
            conflicted_cells.add(key)
            continue
        # Keep corroborating equal observations with distinct IDs. Dropping a
        # later primary-source row would strand provider values without the
        # original-document cross-check required by research readiness.
        accepted.append(fact)
        if not prior:
            cells[key] = fact
    currencies = {
        f["currency"] for f in accepted if f["unit"] in {"currency", "per_share"}
    }
    if len(currencies) > 1:
        errors.append(
            issue("mixed_currency_requires_explicit_conversion", company=issuer)
        )
        accepted = [f for f in accepted if f["unit"] not in {"currency", "per_share"}]
    return accepted, errors


def validate_analysis(
    analysis: dict, packet: dict, calculation_ids: set[str]
) -> list[dict]:
    errors = []
    if not isinstance(analysis, dict):
        return [issue("analysis_not_object", stage="analysis")]
    facts, sources = _references(packet)
    unknown = set(analysis) - {
        "adjustments",
        "conclusions",
        "breakers",
        "scenarios",
        "issues",
    }
    if unknown:
        errors.append(
            issue(
                "unexpected_analysis_fields",
                ",".join(sorted(unknown)),
                stage="analysis",
            )
        )
    conclusions = analysis.get("conclusions", [])
    if (
        not isinstance(conclusions, list)
        or len(conclusions) != 6
        or not all(
            isinstance(c, dict) and isinstance(c.get("key"), str) for c in conclusions
        )
        or {c["key"] for c in conclusions} != CONCLUSION_KEYS
    ):
        return [issue("six_conclusions_required", stage="analysis")]
    for claim in conclusions:
        key = claim["key"]
        extra = set(claim) - {
            "key",
            "status",
            "fact_refs",
            "source_refs",
            "calculation_refs",
            "reason",
            "counterevidence",
            "assumptions",
            "invalidation",
        }
        if extra:
            errors.append(
                issue(
                    "unexpected_conclusion_fields",
                    f"{key}:{','.join(sorted(extra))}",
                    stage="analysis",
                )
            )
        status = claim.get("status")
        if not isinstance(status, str) or status not in CONCLUSION_STATUSES:
            errors.append(issue("invalid_claim_status", key, stage="analysis"))
        if not _text(claim.get("reason")):
            errors.append(
                issue("claim_field_missing", f"{key}.reason", stage="analysis")
            )
        for field in ("counterevidence", "assumptions", "invalidation"):
            values = claim.get(field)
            if (
                not isinstance(values, list)
                or not all(_text(v) for v in values)
                or (field == "invalidation" and not values)
            ):
                errors.append(
                    issue("claim_field_missing", f"{key}.{field}", stage="analysis")
                )
        for field, allowed in (
            ("fact_refs", facts),
            ("source_refs", sources),
            ("calculation_refs", calculation_ids),
        ):
            refs = claim.get(field)
            if not _refs(refs, allowed):
                errors.append(
                    issue("invalid_claim_reference", f"{key}.{field}", stage="analysis")
                )
        if claim.get("status") == "supported" and not claim.get("source_refs"):
            errors.append(issue("unsupported_claim", key, stage="analysis"))
        if status in ("conflict", "insufficient", "refuted"):
            errors.append(issue("unresolved_claim", key, stage="analysis"))
        if status == "not_applicable" and not (
            key == "normalized_cashflow"
            and packet.get("company", {}).get("template") in {"bank", "insurance"}
        ):
            errors.append(
                issue("applicable_claim_marked_not_applicable", key, stage="analysis")
            )
    adjustments = analysis.get("adjustments", [])
    seen, adjustment_ids = set(), set()
    if not isinstance(adjustments, list):
        return errors + [issue("invalid_adjustments", stage="analysis")]
    for adj in adjustments:
        if not isinstance(adj, dict):
            errors.append(issue("invalid_adjustment_object", stage="analysis"))
            continue
        if set(adj) - {
            "id",
            "metric",
            "period_end",
            "amount",
            "unit",
            "currency",
            "source_refs",
            "fact_refs",
            "reason",
        }:
            errors.append(
                issue(
                    "unexpected_adjustment_fields", str(adj.get("id")), stage="analysis"
                )
            )
        if not _refs(adj.get("source_refs"), sources, required=True) or not _refs(
            adj.get("fact_refs"), facts, required=True
        ):
            errors.append(
                issue(
                    "invalid_adjustment_reference", str(adj.get("id")), stage="analysis"
                )
            )
            continue
        key = (
            str(adj.get("metric")),
            str(adj.get("period_end")),
            tuple(sorted(adj["fact_refs"])),
        )
        aid = adj.get("id")
        if (
            not _text(aid)
            or aid in adjustment_ids
            or key in seen
            or not finite(adj.get("amount"))
        ):
            errors.append(
                issue(
                    "invalid_or_duplicate_adjustment",
                    str(adj.get("id")),
                    stage="analysis",
                )
            )
        if isinstance(aid, str):
            adjustment_ids.add(aid)
        seen.add(key)
        if (
            not _text(adj.get("reason"))
            or not _text(adj.get("metric"))
            or not _date(adj.get("period_end"))
        ):
            errors.append(
                issue("unevidenced_adjustment", str(adj.get("id")), stage="analysis")
            )
        if _unit_problem(adj):
            errors.append(issue("invalid_adjustment_unit", str(aid), stage="analysis"))
    for field in ("breakers", "scenarios"):
        if not isinstance(analysis.get(field), list) or not all(
            isinstance(v, dict) for v in analysis[field]
        ):
            errors.append(issue("invalid_analysis_collection", field, stage="analysis"))
            continue
        allowed = (
            {
                "id",
                "metric",
                "operator",
                "threshold",
                "periods",
                "source_refs",
                "reason",
            }
            if field == "breakers"
            else {
                "name",
                "growth_1_5",
                "growth_6_10",
                "reinvestment_rate",
                "source_refs",
            }
        )
        for row in analysis[field]:
            if set(row) - allowed:
                errors.append(
                    issue("unexpected_analysis_item_fields", field, stage="analysis")
                )
            if not _refs(row.get("source_refs"), sources, required=True):
                errors.append(
                    issue("invalid_analysis_item_references", field, stage="analysis")
                )
    if not isinstance(analysis.get("issues"), list) or analysis.get("issues"):
        errors.append(issue("analyst_reported_issues", stage="analysis"))
    return errors


def validate_review(review: dict, packet: dict) -> list[dict]:
    topics = {
        "earnings",
        "cashflow",
        "equity",
        "debt",
        "adjustments",
        "peers",
        "thesis",
    }
    checks = review.get("checks", []) if isinstance(review, dict) else []
    errors = []
    if (
        not isinstance(checks, list)
        or len(checks) != len(topics)
        or not all(
            isinstance(c, dict) and isinstance(c.get("topic"), str) for c in checks
        )
        or {c["topic"] for c in checks} != topics
    ):
        return [issue("incomplete_independent_review", stage="review")]
    facts, sources = _references(packet)
    for check in checks:
        if set(check) - {"topic", "status", "source_refs", "fact_refs", "reason"}:
            errors.append(
                issue("unexpected_review_check_fields", check["topic"], stage="review")
            )
        if check.get("status") != "supported" or not _text(check.get("reason")):
            errors.append(
                issue("review_not_supported", check.get("topic", ""), stage="review")
            )
        if not _refs(check.get("source_refs"), sources, required=True) or not _refs(
            check.get("fact_refs"), facts
        ):
            errors.append(
                issue(
                    "review_reference_invalid", check.get("topic", ""), stage="review"
                )
            )
    if set(review) - {"checks", "issues"}:
        errors.append(issue("unexpected_review_fields", stage="review"))
    if not isinstance(review.get("issues"), list) or review.get("issues"):
        errors.append(issue("review_reported_issues", stage="review"))
    return errors
