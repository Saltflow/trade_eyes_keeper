"""Scoped validation and rendering for independently reviewed PDF sessions.

These checks establish provenance and arithmetic, not accounting truth. A separate
model context must review the images and interpretation. Missing optional history
or peers is a limitation; an invalid reference invalidates its dependants only.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation

from .contracts import CONCLUSION_KEYS, CONCLUSION_STATUSES, fingerprint, finite
from .legacy_reporting import _document, _escape, _section

VERSION = "raw-pdf-sessions/1"
CORE = {"normalized_earnings", "normalized_cashflow", "implied_return"}
MONEY_METRICS = {
    "normalized_earnings",
    "normalized_fcf",
    "normalized_affo",
    "market_cap",
    "operating_cash_flow",
    "capital_expenditures",
    "parent_equity",
    "revenue",
    "net_income_parent",
    "adjusted_net_income_parent",
    "total_assets",
    "total_liabilities",
    "net_debt",
    "cash",
    "affo",
}
RATIO_METRICS = {
    "normalized_roe",
    "normalized_roic",
    "implied_ke_pb",
    "implied_ke_pe",
    "fcf_yield",
    "affo_yield",
    "implied_return",
    "roe",
    "roic",
}


def _currency(value):
    text = str(value or "").strip().lower()
    return {
        "cny": "CNY",
        "rmb": "CNY",
        "人民币": "CNY",
        "人民币元": "CNY",
        "hkd": "HKD",
        "港元": "HKD",
        "港币": "HKD",
        "usd": "USD",
        "美元": "USD",
        "eur": "EUR",
        "欧元": "EUR",
    }.get(text, str(value or "").strip().upper())


def _unit(row):
    """Resolve explicit units, preserving raw records and normalizing operands.

    Numeric references are always base currency units or fractional ratios.
    Thus 10 万元 becomes 100000, and 14 percent becomes 0.14. Currency does not
    convert here: cross-currency conversion still needs an evidenced FX formula.
    """
    expected = (
        "currency"
        if row.get("metric") in MONEY_METRICS
        else ("ratio" if row.get("metric") in RATIO_METRICS else None)
    )
    raw = row.get("unit", expected or "")
    if row.get("record_type") == "qualitative" and raw in (None, "") and not expected:
        return {"unit": "text", "multiplier": 1, "currency": ""}
    if not isinstance(raw, str):
        raise ValueError("invalid unit")  # noqa: TRY004 - structured tool error
    unit = raw.strip().replace(" ", "").lower()
    ratio = {
        "ratio": 1,
        "fraction": 1,
        "%": 0.01,
        "percent": 0.01,
        "percentage": 0.01,
        "百分比": 0.01,
        "百分率": 0.01,
    }
    if unit in ratio:
        if expected == "currency":
            raise ValueError("monetary metric cannot use ratio unit")
        return {"unit": "ratio", "multiplier": ratio[unit], "currency": ""}
    multiplier = 1
    for label, scale in (
        ("billion", 1e9),
        ("million", 1e6),
        ("thousand", 1e3),
        ("千万", 1e7),
        ("百万", 1e6),
        ("亿", 1e8),
        ("万", 1e4),
        ("千", 1e3),
    ):
        if label in unit:
            unit = unit.replace(label, "", 1)
            multiplier = scale
            break
    money = {
        "currency",
        "元",
        "cny",
        "rmb",
        "hkd",
        "usd",
        "eur",
        "人民币",
        "人民币元",
        "港元",
        "港币",
        "美元",
        "欧元",
    }
    if unit in money:
        if expected == "ratio":
            raise ValueError("ratio metric cannot use currency unit")
        currency = _currency(row.get("currency"))
        explicit = "" if unit in {"currency", "元"} else _currency(unit)
        if explicit and currency and explicit != currency:
            raise ValueError("unit currency contradicts declared currency")
        return {
            "unit": "currency",
            "multiplier": multiplier,
            "currency": currency or explicit,
        }
    if expected:
        raise ValueError("metric requires explicit monetary or ratio unit")
    return {"unit": raw, "multiplier": 1, "currency": _currency(row.get("currency"))}


def calculate(expression, values: dict, *, _depth=0) -> float:
    """Evaluate a bounded declarative expression; no Python or string evaluation.

    A leaf is a finite number or {"ref": "fact-id"}. Operators use
    {"op": "add|sub|mul|div|min|max|mean", "args": [...]}; explicit aliases
    subtract/multiply/divide/sum/minimum/maximum/average are equivalent. Values may be
    scalar facts or named scenario values, e.g. "owner_cash.low".
    """
    if _depth > 32:
        raise ValueError("calculation nesting limit")
    if finite(expression):
        return float(expression)
    if not isinstance(expression, dict):
        raise ValueError("invalid expression")  # noqa: TRY004 - one tool error type
    if set(expression) == {"ref"}:
        ref = expression["ref"]
        if not isinstance(ref, str) or not finite(values.get(ref)):
            raise ValueError("unknown or nonnumeric calculation reference")
        return float(values[ref])
    if set(expression) != {"op", "args"}:
        raise ValueError("invalid expression fields")
    args = expression["args"]
    if not isinstance(args, list) or not 1 <= len(args) <= 100:
        raise ValueError("invalid argument count")
    items = [Decimal(str(calculate(v, values, _depth=_depth + 1))) for v in args]
    op = expression["op"]
    if not isinstance(op, str):
        raise ValueError("invalid operator")  # noqa: TRY004
    op = op.strip().lower()
    op = {
        "subtract": "sub",
        "multiply": "mul",
        "divide": "div",
        "sum": "add",
        "minimum": "min",
        "maximum": "max",
        "average": "mean",
    }.get(op, op)
    if op == "add":
        result = sum(items)
    elif op == "mul":
        result = Decimal(1)
        for item in items:
            result *= item
    elif op == "sub" and len(items) == 2:
        result = items[0] - items[1]
    elif op == "div" and len(items) == 2 and items[1] != 0:
        result = items[0] / items[1]
    elif op == "min":
        result = min(items)
    elif op == "max":
        result = max(items)
    elif op == "mean":
        result = sum(items) / len(items)
    else:
        raise ValueError("unsupported operation or invalid operands")
    value = float(result)
    if not finite(value):
        raise ValueError("nonfinite calculation")
    return value


def _record_ref(ref):
    prefix, separator, suffix = ref.rpartition(".")
    return prefix if separator and suffix in {"low", "base", "high"} else ref


def _refs(expression, depth=0):
    if not isinstance(expression, dict) or depth > 32:
        return set()
    if isinstance(expression.get("ref"), str):
        return {_record_ref(expression["ref"])}
    args = expression.get("args", [])
    if not isinstance(args, list):
        return set()
    return set().union(*(_refs(v, depth + 1) for v in args))


def _strings(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def evaluate_round(
    output: dict,
    review: dict,
    pages: list[dict],
    *,
    company_id: str,
    template: str = "ordinary",
    independently_reviewed: bool = False,
    _calculation_preview: dict | None = None,
) -> dict:
    """Validate a round and return a safe, conclusion-scoped display snapshot.

    ``pages`` is a trusted manifest of pages actually supplied/read by the two
    contexts, never model-supplied page IDs. The caller attests independent
    execution; publication additionally freezes the input hash for replay.
    ``report_complete`` expresses the researcher's final-report intention.
    """
    output = output if isinstance(output, dict) else {}
    review = review if isinstance(review, dict) else {}
    issues, invalid, values, records, ranges, normalization = [], set(), {}, {}, {}, {}
    recomputed = {}
    dependencies, record_kinds = {}, {}
    estimate_ids = (
        {
            r["id"]
            for r in output.get("estimates", [])
            if isinstance(r, dict) and isinstance(r.get("id"), str)
        }
        if isinstance(output.get("estimates", []), list)
        else set()
    )

    def metadata_refs(field, refs):
        return [
            _record_ref(ref)
            if field == "estimate_refs" and _record_ref(ref) in estimate_ids
            else ref
            for ref in refs
        ]

    conclusions = output.get("conclusions", [])
    conclusions = conclusions if isinstance(conclusions, list) else []
    claims = {
        c["key"]: copy.deepcopy(c)
        for c in conclusions
        if isinstance(c, dict)
        and isinstance(c.get("key"), str)
        and c["key"] in CONCLUSION_KEYS
    }
    page_map = {p["id"]: p for p in pages if isinstance(p, dict) and p.get("id")}

    def problem(code, reason, affected=(), record_id=None):
        if record_id:
            invalid.add(record_id)
        issues.append(
            {
                "code": code,
                "severity": "blocking",
                "reason": reason,
                "affected_conclusions": sorted(set(affected)),
                "record_id": record_id,
            }
        )

    def page_check(row, affected=(), required=False, fact=False):
        refs = row.get("page_refs", [])
        if not _strings(refs) or (required and not refs):
            problem(
                "missing_page_reference",
                "原文页引用缺失或格式错误",
                affected,
                row.get("id"),
            )
            return
        for ref in refs:
            if ref not in page_map:
                problem("unknown_page_reference", ref, affected, row.get("id"))
                continue
            page = page_map[ref]
            for field in ("company_id", "period_end", "currency"):
                declared, expected = row.get(field), page.get(field)
                if not fact and field in {"company_id", "period_end"}:
                    continue
                if field == "currency":
                    if fact and row.get("record_type") == "qualitative":
                        # A quoted management explanation has no monetary unit.
                        # Numeric metrics/values disguised as qualitative remain
                        # rejected by the record contract below.
                        continue
                    if not fact:
                        # An explicit estimate may translate source currencies;
                        # the reviewer checks its method and FX assumptions.
                        # Disclosed facts never get that interpretation freedom.
                        continue
                    try:
                        if _unit(row)["unit"] == "ratio":
                            continue
                    except ValueError:
                        pass
                    declared, expected = _currency(declared), _currency(expected)
                # A document's fiscal end is not the only period printed on its
                # pages. Comparative columns are valid unless an authoritative
                # per-page period manifest explicitly says otherwise.
                allowed = page.get(
                    f"{field}s",
                    [] if field == "period_end" else ([expected] if expected else []),
                )
                if field == "currency":
                    allowed = [_currency(v) for v in allowed]
                if declared and allowed and declared not in allowed:
                    problem(
                        f"{field}_mismatch", f"{ref}: {field}", affected, row.get("id")
                    )
                if field == "period_end" and declared and fact:
                    try:
                        period = date.fromisoformat(declared)
                        if expected and period > date.fromisoformat(expected):
                            raise ValueError("fact period beyond report")
                    except (ValueError, TypeError):
                        problem("period_end_mismatch", ref, affected, row.get("id"))
                if fact and field == "period_end" and page.get("kind") == "quote":
                    quote_date = str(
                        page.get("quote_date") or expected or page.get("as_of") or ""
                    )[:10]
                    if quote_date and declared != quote_date:
                        problem("quote_date_mismatch", ref, affected, row.get("id"))

    if output.get("company_id", company_id) != company_id:
        problem("company_mismatch", "研究对象不匹配", CONCLUSION_KEYS)
    if not independently_reviewed or not review:
        problem("independent_review_missing", "缺少独立上下文审查", CONCLUSION_KEYS)
    if len(claims) != len(conclusions):
        problem("invalid_conclusion_keys", "未知或重复结论", CONCLUSION_KEYS)

    for kind in ("facts", "estimates", "calculations"):
        rows = output.get(kind, [])
        if not isinstance(rows, list):
            problem("invalid_records", kind, CONCLUSION_KEYS)
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                problem("invalid_record", kind, CONCLUSION_KEYS)
                continue
            rid = row["id"]
            if not rid or _record_ref(rid) != rid:
                problem("reserved_or_empty_record_id", rid, CONCLUSION_KEYS)
                continue
            if rid in records:
                problem("duplicate_record", rid, record_id=rid)
                continue
            records[rid] = row
            record_kinds[rid] = kind
            try:
                normalization[rid] = _unit(row)
            except ValueError as exc:
                problem("metric_unit_mismatch", str(exc), record_id=rid)
                normalization[rid] = {
                    "unit": "invalid",
                    "multiplier": 1,
                    "currency": "",
                }
            factor = normalization[rid]["multiplier"]
            dep = set()
            for field in ("fact_refs", "estimate_refs", "calculation_refs"):
                refs = row.get(field, [])
                if not _strings(refs):
                    problem("invalid_record_references", rid, record_id=rid)
                else:
                    dep.update(metadata_refs(field, refs))
            dependencies[rid] = dep
            page_check(
                row, required=kind in {"facts", "estimates"}, fact=kind == "facts"
            )
            if kind == "facts":
                record_type = row.get("record_type", "quantitative")
                identity_fields = (
                    ("company_id", "period_end")
                    if record_type == "qualitative"
                    else (
                        ("company_id", "period_end", "unit")
                        if normalization[rid]["unit"] == "ratio"
                        else ("company_id", "period_end", "unit", "currency")
                    )
                )
                if not all(_text(row.get(k)) for k in identity_fields):
                    problem("fact_identity_missing", rid, record_id=rid)
                if record_type == "qualitative":
                    if (
                        not _text(row.get("quote"))
                        or not _text(row.get("metric"))
                        or row.get("metric") in MONEY_METRICS | RATIO_METRICS
                        or normalization[rid]["unit"] in {"currency", "ratio"}
                        or (row.get("value") is not None and not _text(row["value"]))
                    ):
                        problem("invalid_qualitative_fact", rid, record_id=rid)
                elif record_type != "quantitative":
                    problem("invalid_fact_record_type", rid, record_id=rid)
                elif not finite(row.get("value")):
                    problem("invalid_fact_value", rid, record_id=rid)
                else:
                    values[rid] = row["value"] * factor
                    if not finite(values[rid]):
                        problem("nonfinite_normalized_value", rid, record_id=rid)
            elif kind == "estimates":
                if not all(
                    _text(row.get(k))
                    for k in ("company_id", "period_end", "currency", "unit")
                ):
                    problem("estimate_identity_missing", rid, record_id=rid)
                try:
                    date.fromisoformat(row.get("period_end", ""))
                except (ValueError, TypeError):
                    problem("estimate_period_invalid", rid, record_id=rid)
                allowed_companies = {company_id} | {
                    p.get("company_id")
                    for p in page_map.values()
                    if isinstance(p.get("company_id"), str)
                }
                if (
                    not isinstance(row.get("company_id"), str)
                    or row["company_id"] not in allowed_companies
                ):
                    problem("estimate_company_unknown", rid, record_id=rid)
                scenario = [row.get(k) for k in ("low", "base", "high")]
                if not all(finite(v) for v in scenario) or scenario != sorted(scenario):
                    problem("invalid_estimate_range", rid, record_id=rid)
                else:
                    values.update(
                        {f"{rid}.{k}": row[k] * factor for k in ("low", "base", "high")}
                    )
                    values[rid] = row["base"] * factor
                    ranges[rid] = {k: row[k] * factor for k in ("low", "base", "high")}
                    if not all(finite(v) for v in ranges[rid].values()):
                        problem("nonfinite_normalized_value", rid, record_id=rid)
                if not all(
                    _text(row.get(k)) or (_strings(row.get(k)) and bool(row[k]))
                    for k in ("method", "sensitivity", "invalidation", "unit")
                ):
                    problem("unsupported_estimate", rid, record_id=rid)
            else:
                expression = row.get("expression")
                dependencies[rid].update(_refs(expression))
                try:
                    scenarios = row.get("scenarios")
                    if scenarios is not None:
                        if not isinstance(scenarios, dict) or set(scenarios) != {
                            "low",
                            "base",
                            "high",
                        }:
                            raise ValueError(
                                "scenario expressions require low/base/high"
                            )
                        for scenario in scenarios.values():
                            dependencies[rid].update(_refs(scenario))
                        scenario_values = {
                            k: calculate(scenarios[k], values)
                            for k in ("low", "base", "high")
                        }
                        if list(scenario_values.values()) != sorted(
                            scenario_values.values()
                        ):
                            raise ValueError("scenario results not ordered")
                        calculated = scenario_values["base"]
                        if (
                            expression is not None
                            and calculate(expression, values) != calculated
                        ):
                            raise ValueError(
                                "base expression differs from scenario base"
                            )
                        ranges[rid] = scenario_values
                    else:
                        calculated = calculate(expression, values)
                    normalized_value = calculated * factor
                    normalized_range = {
                        k: v * factor for k, v in ranges.get(rid, {}).items()
                    }
                    if finite(normalized_value) and all(
                        finite(v) for v in normalized_range.values()
                    ):
                        recomputed[rid] = {
                            "value": normalized_value,
                            "range": normalized_range or None,
                            "unit": normalization[rid]["unit"],
                            "currency": normalization[rid]["currency"],
                            "proposed_value": row.get("value"),
                        }
                    places = row.get("decimal_places", 8)
                    if (
                        not isinstance(places, int)
                        or isinstance(places, bool)
                        or not 0 <= places <= 12
                    ):
                        raise ValueError("invalid declared precision")
                    supplied = row.get("value")
                    if supplied is not None and (
                        not finite(supplied)
                        or abs(Decimal(str(supplied)) - Decimal(str(calculated)))
                        > Decimal(10) ** -places / 2
                    ):
                        raise ValueError(
                            "calculation does not reproduce claimed result"
                        )
                    values[rid] = calculated * factor
                    if rid in ranges:
                        ranges[rid] = {k: v * factor for k, v in ranges[rid].items()}
                    if not finite(values[rid]) or not all(
                        finite(v) for v in ranges.get(rid, {}).values()
                    ):
                        raise ValueError("nonfinite normalized calculation")
                except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                    problem("calculation_error", str(exc), record_id=rid)

    # Dependency closure: an invalid leaf must not reappear via another metric.
    for _ in range(len(records) + 1):
        previous = set(invalid)
        for rid, deps in dependencies.items():
            if rid not in invalid and (deps - set(records) or deps & invalid):
                problem(
                    "invalid_record_dependency",
                    f"{rid}: {sorted((deps - set(records)) | (deps & invalid))}",
                    record_id=rid,
                )
        if invalid == previous:
            break

    if _calculation_preview is not None:
        _calculation_preview.update(
            {
                "scope": "deterministic_only_not_reviewed",
                "calculations": recomputed,
                "values": {
                    k: v for k, v in values.items() if _record_ref(k) not in invalid
                },
                "normalization": copy.deepcopy(normalization),
                "invalid_records": sorted(invalid),
                "errors": [
                    copy.deepcopy(i)
                    for i in issues
                    if i["code"] != "independent_review_missing"
                ],
            }
        )

    def has_target_original(rid, seen=frozenset()):
        if rid in seen or rid not in records:
            return False
        refs = records[rid].get("page_refs", [])
        if _strings(refs) and any(
            page_map.get(ref, {}).get("company_id") == company_id
            and page_map.get(ref, {}).get("kind") != "quote"
            for ref in refs
        ):
            return True
        return any(
            has_target_original(dep, seen | {rid})
            for dep in dependencies.get(rid, set())
        )

    def has_foreign_dependency(rid, seen=frozenset()):
        """Peer data can inform explicit assumptions, never replace own facts.

        Target estimates may declare peer_assumption_refs. That creates a visible
        assumption boundary requiring the review's existing source coverage; a
        formula cannot silently acquire target identity by writing company_id.
        """
        # Unknown references and arithmetic errors are already diagnosed above.
        # They are not evidence that another issuer's data was used.
        if rid in seen or rid not in records:
            return False
        row, kind = records[rid], record_kinds[rid]
        if kind == "facts":
            return _text(row.get("company_id")) and row["company_id"] != company_id
        deps = dependencies.get(rid, set())
        if kind == "estimates":
            if _text(row.get("company_id")) and row["company_id"] != company_id:
                return True
            assumed = row.get("peer_assumption_refs", [])
            if (
                row.get("company_id") != company_id
                or not _strings(assumed)
                or not set(assumed) <= deps
            ):
                assumed = []
            return any(
                dep not in assumed and has_foreign_dependency(dep, seen | {rid})
                for dep in deps
            )
        if _text(row.get("company_id")) and row["company_id"] != company_id:
            return True
        return any(has_foreign_dependency(dep, seen | {rid}) for dep in deps)

    review_rows = review.get("conclusions", [])
    review_rows = review_rows if isinstance(review_rows, list) else []
    audits = {
        r["key"]: r
        for r in review_rows
        if isinstance(r, dict)
        and isinstance(r.get("key"), str)
        and r["key"] in CONCLUSION_KEYS
    }
    if len(audits) != len(review_rows):
        problem("invalid_review_keys", "未知或重复审查结论", CONCLUSION_KEYS)
    for key, claim in claims.items():
        if (
            not isinstance(claim.get("status"), str)
            or claim["status"] not in CONCLUSION_STATUSES
            or not _text(claim.get("reason"))
        ):
            problem("invalid_conclusion", key, [key])
        decisive = isinstance(claim.get("status"), str) and claim["status"] in {
            "supported",
            "refuted",
        }
        page_check(claim, [key], required=decisive)
        deps = set()
        for field in ("fact_refs", "estimate_refs", "calculation_refs"):
            refs = claim.get(field, [])
            if not _strings(refs):
                problem("invalid_claim_references", field, [key])
            else:
                normalized_refs = metadata_refs(field, refs)
                deps.update(normalized_refs)
                claim[field] = normalized_refs
        if deps - set(records) or deps & invalid:
            problem("invalid_claim_dependency", key, [key])
        if key in CORE | {"capital_efficiency"} and any(
            has_foreign_dependency(rid) for rid in deps
        ):
            problem(
                "core_company_scope_mismatch",
                "本公司核心指标引用了同行数值；同行只能经明确披露且已审查的本公司估算假设参与计算。",
                [key],
            )
        if (
            key in {"normalized_earnings", "normalized_cashflow", "capital_efficiency"}
            and decisive
            and deps
            and not any(has_target_original(rid) for rid in deps)
        ):
            problem(
                "target_original_basis_missing",
                "经营核心估算没有本公司原文依据；行情或同行资料不能单独充当本公司财务基础。",
                [key],
            )
        audit = audits.get(key, {})
        if (
            not isinstance(audit.get("verdict"), str)
            or audit["verdict"] not in {"accepted", "limited", "blocked"}
            or not _text(audit.get("reason"))
        ):
            problem("claim_not_reviewed", key, [key])
        elif audit["verdict"] == "blocked":
            problem("review_blocked", audit["reason"], [key])
        page_check(audit, [key], required=decisive)
        if decisive and _strings(audit.get("page_refs")):
            used = set(deps)
            for _ in range(len(records)):
                expanded = used | set().union(
                    *(dependencies.get(r, set()) for r in used)
                )
                if expanded == used:
                    break
                used = expanded
            required_pages = (
                set(claim.get("page_refs", []))
                if _strings(claim.get("page_refs", []))
                else set()
            )
            for rid in used:
                refs = records.get(rid, {}).get("page_refs", [])
                if _strings(refs):
                    required_pages.update(refs)
            missing_pages = sorted(required_pages - set(audit["page_refs"]))
            if missing_pages:
                problem(
                    "review_evidence_coverage_missing",
                    f"{key}: 审查未覆盖这些依赖原页：{', '.join(missing_pages)}",
                    [key],
                )
                issues[-1]["missing_page_refs"] = missing_pages
        claim["source_refs"] = claim.get("page_refs", [])
    for issue in (
        review.get("issues", []) if isinstance(review.get("issues", []), list) else [{}]
    ):
        affected = (
            issue.get("affected_conclusions", []) if isinstance(issue, dict) else []
        )
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get("severity"), str)
            or issue["severity"] not in {"blocking", "limitation"}
            or not _text(issue.get("reason"))
            or not _strings(affected)
            or not affected
            or not set(affected) <= CONCLUSION_KEYS
        ):
            problem("invalid_review_issue", "审查问题必须说明影响范围", CONCLUSION_KEYS)
            continue
        page_check(issue, affected)
        issues.append(copy.deepcopy(issue))

    blocked = {
        k
        for i in issues
        if i["severity"] == "blocking"
        for k in i["affected_conclusions"]
    }
    for key in blocked & set(claims):
        claims[key] = {
            "key": key,
            "status": "conflict",
            "reason": "该结论存在未解决错误，原判断已隔离。",
            "limitations": [
                i["reason"] for i in issues if key in i["affected_conclusions"]
            ],
        }
    core = set(CORE)
    if template in {"bank", "insurance"}:
        core = {"normalized_earnings", "capital_efficiency", "implied_return"}
    core_ok = all(
        claims.get(k, {}).get("status") in {"supported", "refuted"} and k not in blocked
        for k in core
    )
    # Core numerical conclusions must expose their calculation/assumption inputs.
    metric_groups = {
        "normalized_earnings": {"normalized_earnings", "normalized_affo"},
        "normalized_cashflow": {"normalized_fcf", "normalized_affo"},
        "capital_efficiency": {"normalized_roe", "normalized_roic"},
        "implied_return": {
            "implied_ke_pb",
            "implied_ke_pe",
            "fcf_yield",
            "affo_yield",
            "implied_return",
        },
    }
    core_ok = core_ok and all(
        any(
            records.get(r, {}).get("metric") in metric_groups[k] and r not in invalid
            for field in ("estimate_refs", "calculation_refs")
            for r in claims.get(k, {}).get(field, [])
        )
        for k in core
    )
    core_ok = core_ok and any(
        r in ranges
        and records.get(r, {}).get("metric") in metric_groups["implied_return"]
        and r not in invalid
        and isinstance(records.get(r, {}).get("scenarios"), dict)
        for r in claims.get("implied_return", {}).get("calculation_refs", [])
    )
    ready = bool(
        output.get("report_complete") is True
        and set(claims) == CONCLUSION_KEYS
        and core_ok
        and independently_reviewed
        and isinstance(output.get("action"), str)
        and output["action"] in {"candidate", "observe", "research", "abandon", "defer"}
    )
    if output.get("report_complete") is True and not core_ok:
        issues.append(
            {
                "code": "final_core_evidence_incomplete",
                "severity": "limitation",
                "affected_conclusions": sorted(core),
                "reason": "完整报告尚缺核心数值、可重算回报区间或有效审查；请补证、计算区间或明确仅为阶段结果。",
            }
        )
    # Only display records actually covered by a surviving substantive review.
    # All other proposals remain in the immutable round, not the public tables.
    reviewed_records = set()
    for key, claim in claims.items():
        if key in blocked or claim.get("status") not in {"supported", "refuted"}:
            continue
        for field in ("fact_refs", "estimate_refs", "calculation_refs"):
            reviewed_records.update(claim.get(field, []))
    for _ in range(len(records)):
        expanded = reviewed_records | set().union(
            *(dependencies.get(rid, set()) for rid in reviewed_records)
        )
        if expanded == reviewed_records:
            break
        reviewed_records = expanded
    reviewed_records -= invalid
    return {
        "version": VERSION,
        "company_id": company_id,
        "template": template,
        "status": "reviewed_report" if ready else ("blocked" if blocked else "stage"),
        "report_ready": ready,
        "candidate_ready": ready and output.get("action") == "candidate",
        "conclusions": list(claims.values()),
        "issues": issues,
        "blocking": sorted(blocked),
        "invalid_records": sorted(invalid),
        "reviewed_records": sorted(reviewed_records),
        "qualitative_facts": [
            copy.deepcopy(records[rid])
            for rid in sorted(reviewed_records)
            if records[rid].get("record_type") == "qualitative"
        ],
        "calculations": {
            k: v for k, v in values.items() if _record_ref(k) in reviewed_records
        },
        "ranges": {k: v for k, v in ranges.items() if k in reviewed_records},
        "normalization": {
            k: v for k, v in normalization.items() if k in reviewed_records
        },
        "review": copy.deepcopy(review),
    }


def deterministic_calculation_preview(
    output, pages, *, company_id, template="ordinary"
):
    """Prepare code diagnostics for a reviewer without inventing review approval.

    The numeric path is exactly the one later used by publication validation.
    A recomputed value can appear alongside an error when the model's proposed
    value disagrees; such diagnostic values are not qualified report metrics.
    """
    preview = {}
    evaluate_round(
        output,
        {},
        pages,
        company_id=company_id,
        template=template,
        independently_reviewed=False,
        _calculation_preview=preview,
    )
    return preview


def report_payload(company, result, output, review, pages, usage, *, as_of=""):
    """Compatible immutable publication envelope for existing report/notice code."""
    company = copy.deepcopy(company)
    snapshot = {
        "output": output,
        "review": review,
        "pages": pages,
        "company_id": company["id"],
        "template": company.get("template", result.get("template", "ordinary")),
        "independently_reviewed": True,
    }
    valid = set(result.get("calculations", {}))
    visible_facts = valid | {r["id"] for r in result.get("qualitative_facts", [])}
    facts = [
        dict(f, source_id=f["page_refs"][0])
        for f in output.get("facts", [])
        if f.get("id") in visible_facts
    ]
    return {
        "policy_version": VERSION,
        "company": company,
        "as_of": as_of,
        "input_hash": fingerprint(snapshot),
        "session_input_hash": fingerprint(snapshot),
        "session_round": copy.deepcopy(snapshot),
        "decision": {
            "action": output.get("action"),
            "reason": output.get("reason", ""),
        },
        "candidate": result.get("candidate_ready", False),
        "qualification": "reviewed_candidate"
        if result.get("candidate_ready")
        else "reviewed_non_candidate",
        "quote_date": max(
            (
                str(p.get("quote_date") or p.get("period_end") or p.get("as_of") or "")[
                    :10
                ]
                for p in pages
                if p.get("kind") == "quote"
            ),
            default=None,
        ),
        "conclusions": copy.deepcopy(result["conclusions"]),
        "coverage": output.get("coverage", {}),
        "metrics": {
            r["id"]: dict(
                r,
                value=result["calculations"][r["id"]],
                range=result.get("ranges", {}).get(r["id"]),
                unit=result["normalization"][r["id"]]["unit"],
                currency=result["normalization"][r["id"]]["currency"],
                proposed_unit=r.get("unit"),
                proposed_value=r.get("value"),
            )
            for r in output.get("calculations", [])
            if r.get("id") in valid
        },
        "estimates": [e for e in output.get("estimates", []) if e.get("id") in valid],
        "evidence_packet": {
            "company": company,
            "facts": facts,
            "sources": copy.deepcopy(pages),
        },
        "source_refs": sorted(
            {ref for c in result["conclusions"] for ref in c.get("page_refs", [])}
            | {
                ref
                for field in ("facts", "estimates", "calculations")
                for record in output.get(field, [])
                if record.get("id") in valid
                for ref in record.get("page_refs", [])
            }
        ),
        "fact_refs": [f["id"] for f in facts],
        "review": copy.deepcopy(review),
        "usage": copy.deepcopy(usage),
    }


def accept_publication(payload):
    """Replay the immutable round; do not trust a cached report_ready flag."""
    frozen = payload.get("session_round")
    if not isinstance(frozen, dict) or fingerprint(frozen) != payload.get(
        "session_input_hash"
    ):
        return [{"code": "session_evidence_hash_mismatch"}]
    if frozen.get("company_id") != payload.get("company", {}).get("id"):
        return [{"code": "session_company_mismatch"}]
    try:
        result = evaluate_round(**frozen)
    except (TypeError, ValueError, KeyError):
        return [{"code": "invalid_session_round"}]
    if not result["report_ready"]:
        return [{"code": "session_report_incomplete", "issues": result["issues"]}]
    if result["conclusions"] != payload.get("conclusions"):
        return [{"code": "session_conclusion_mismatch"}]
    expected = report_payload(
        payload["company"],
        result,
        frozen["output"],
        frozen["review"],
        frozen["pages"],
        payload.get("usage"),
    )
    for field in (
        "metrics",
        "estimates",
        "evidence_packet",
        "decision",
        "review",
        "candidate",
        "qualification",
        "quote_date",
        "source_refs",
        "fact_refs",
        "coverage",
    ):
        if payload.get(field) != expected[field]:
            return [{"code": "session_report_content_mismatch", "field": field}]
    return []


def render_round_html(result, output, *, title="原文研究阶段结果", usage=None):
    """Render scoped, reviewed claims only; never leak rejected original prose."""
    return _document(
        title,
        f"<p>状态：{_escape(result['status'])}；阶段记录不等于有效报告。</p>"
        + _section("已读范围", output.get("coverage"))
        + _section("逐项结论", result["conclusions"])
        + _section("独立审查与未解决问题", result["issues"])
        + _section("代码计算", result["calculations"])
        + _section("已审查定性事实", result.get("qualitative_facts"))
        + _section("计算单位与换算", result.get("normalization"))
        + _section("实际调用与 token", usage),
    )
