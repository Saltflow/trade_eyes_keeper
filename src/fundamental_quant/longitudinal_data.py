"""Reusable offline comparison inputs; no authored judgments or network calls.

Facts retain their disclosure versions. Retrospective conflicts stay unavailable;
point-in-time valuations select only statements disclosed before the trading date.
"""

from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .fr_pack import (
    extract_field,
    make_pack,
    read_json,
    validate_source,
    verified_pack,
    write_json,
)
from .fr_pack_contract import FIELDS, PARSER_VERSION
from .fr_pack_gate import assess_materials

VERSION = "longitudinal-data/5"
STOCK_FIELDS = {k for k, v in FIELDS.items() if v[1] == "balance"} | {
    "shares_outstanding"
}
CORE_GROWTH = ("revenue", "net_profit_parent", "cfo", "fcf_proxy")


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _ratio(a, b):
    return None if a is None or b is None or b <= 0 else a / b


def _growth(a, b, years):
    return (
        None
        if a is None or b is None or a <= 0 or b <= 0 or years <= 0
        else (b / a) ** (1 / years) - 1
    )


def _number(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite_financial_value")
    return float(result)


def source_from_pdf(metadata: dict) -> dict:
    """Convert an existing identified PDF locally; never invent disclosure dates.

    Caller supplies issuer_id, company_name, period_end, published_at, path, sha256.
    Original pages (including empty ones) remain available for image fallback.
    """
    required = (
        "issuer_id",
        "company_name",
        "period_end",
        "published_at",
        "path",
        "sha256",
    )
    if any(not metadata.get(k) for k in required):
        raise ValueError("pdf_source_metadata_incomplete")
    meta = dict(metadata)
    pdf = Path(meta["path"])
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != meta["sha256"]:
        raise ValueError("pdf_source_hash_mismatch")
    _day(meta["period_end"])
    _day(meta["published_at"])
    import pypdfium2

    pages = {}
    with pypdfium2.PdfDocument(str(pdf)) as doc:
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            try:
                pages[str(i + 1)] = textpage.get_text_range()
            finally:
                textpage.close()
                page.close()
    cover = "".join("".join(pages.get(str(p), "").split()) for p in (1, 2))
    legal_name = meta.get("legal_name", meta["company_name"])
    cover_matches = "".join(legal_name.split()) in cover
    if not cover_matches:
        import re

        code = meta["issuer_id"].split(":")[-1]
        identity_matches = []
        for page_no in range(1, min(len(pages), 20) + 1):
            compact_page = "".join(pages[str(page_no)].split())
            for match in re.finditer("股票简况|股票代码|股份代号", compact_page):
                window = compact_page[max(0, match.start() - 50) : match.end() + 500]
                if "".join(meta["company_name"].split()) in window and re.search(
                    r"(?<![0-9])" + re.escape(code) + r"(?![0-9])", window
                ):
                    identity_matches.append(
                        {
                            "page": page_no,
                            "quote": window,
                            "method": "stock_profile_name_and_security_code",
                        }
                    )
        if not identity_matches:
            raise ValueError("pdf_cover_company_not_matched")
        meta["identity_evidence"] = identity_matches
    if str(meta["period_end"])[:4] not in cover:
        raise ValueError("pdf_cover_period_not_matched")
    meta.setdefault("document_id", f"pdf_{meta['sha256']}")
    meta.setdefault("short_id", meta["document_id"])
    meta.setdefault(
        "kind", "annual" if meta["period_end"].endswith("12-31") else "interim"
    )
    meta["page_count"] = len(pages)
    meta["sparse_pages"] = [int(p) for p, text in pages.items() if not text.strip()]
    return {"metadata": meta, "pages": pages}


def _presentation_currency_disclosure(source: dict, document_id: str) -> dict | None:
    """Resolve only an explicit statement-wide presentation currency declaration.

    Functional currency, foreign-currency note rows and RMB transaction amounts
    do not identify presentation currency. No declaration is carried across files.
    """
    currencies = {"人民币": "CNY", "美元": "USD", "港元": "HKD", "欧元": "EUR"}
    templates = (
        r"本集团编制本财务报表时所采用的货币为(?P<currency>人民币|美元|港元|欧元)[。；]",
        r"本(?:合并)?财务报表(?:以|采用)(?P<currency>人民币|美元|港元|欧元)(?:列报|编制)[。；]",
    )
    declarations = []
    for page, original in source.get("pages", {}).items():
        positions = [i for i, char in enumerate(original) if not char.isspace()]
        compact = "".join(original[i] for i in positions)
        for template in templates:
            for match in re.finditer(template, compact):
                # Preserve the exact source text, including line wrapping.
                quote = original[
                    positions[match.start()] : positions[match.end() - 1] + 1
                ]
                declarations.append(
                    {
                        "method": "explicit_statement_presentation_currency/v1",
                        "currency": currencies[match.group("currency")],
                        "quote": quote,
                        "refs": [f"{document_id}:p{page}"],
                        "scope": "this_document_consolidated_financial_statements",
                    }
                )
    if not declarations or len({x["currency"] for x in declarations}) != 1:
        return None
    return declarations[0]


def _restatement_disclosure(source: dict, document_id: str) -> dict | None:
    """Require the actual selected YES plus a stated reason, never policy boilerplate.

    An affirmative financial-summary disclosure authorizes the document's own
    comparative columns for retrospective presentation, not earlier PIT history.
    """
    pages = source.get("pages", {})
    for page in sorted(int(key) for key in pages if str(key).isdigit()):
        text = pages[str(page)]
        if "公司是否需追溯调整或重述以前年度会计数据" not in text:
            continue
        following = pages.get(str(page + 1), "")
        combined = text + ("\n" + following if following else "")
        match = re.search(
            r"公司是否需追溯调整或重述以前年度会计数据\s*[\uf052☑√✓✔■]\s*是\s*□\s*否"
            r"[\s\S]{0,180}?追溯调整或重述原因\s*([^\r\n]+)",
            combined,
        )
        if not match:
            continue
        after = combined[match.end() : match.end() + 800]
        if "调整后" not in after:
            continue
        refs = [f"{document_id}:p{page}"]
        if match.end() > len(text):
            refs.append(f"{document_id}:p{page + 1}")
        return {
            "method": "checked_affirmative_restatement_disclosure/v1",
            "reason": match.group(1).strip(),
            "quote": match.group(0),
            "refs": refs,
            "report_period": source.get("metadata", {}).get("period_end"),
            "published_at": source.get("metadata", {}).get("published_at"),
            "scope": "this_document_comparative_financial_statements_only",
        }
    # SSE reports may disclose adjustment columns rather than the SZSE question.
    # Require all three: actual before/after comparative amounts, a current-year
    # merger event, and the policy that explicitly adjusts comparative statements.
    proof = {}
    patterns = {
        "event": r"(?:(?:本年|本期|报告期)(?:发生了?|新增|增加)同一控制下企业合并|(?:本年|本期|报告期)同一控制下企业合并增加)[^。；]{3,240}[。；]",
        "policy": r"对于同一控制下企业合并取得的子公司[^。]{0,120}。编制比较合并财务报表时，对上年财务报表的相关项目进行调整[^。]{0,120}。",
    }
    for page, original in pages.items():
        positions = [i for i, char in enumerate(original) if not char.isspace()]
        compact = "".join(original[i] for i in positions)
        if (
            "主要会计数据" in compact
            and "调整前" in compact
            and "调整后" in compact
            and "营业收入" in compact
            and "经营活动产生的现金流" in compact
        ):
            proof.setdefault("columns", {"page": page, "quote": original})
        for name, pattern in patterns.items():
            match = re.search(pattern, compact)
            if match:
                quote = original[
                    positions[match.start()] : positions[match.end() - 1] + 1
                ]
                proof.setdefault(name, {"page": page, "quote": quote})
    if set(proof) == {"columns", "event", "policy"}:
        return {
            "method": "adjusted_comparatives_with_current_merger_evidence/v1",
            "reason": proof["event"]["quote"],
            "quote": "\n".join(
                proof[k]["quote"] for k in ("columns", "event", "policy")
            ),
            "evidence": [proof[k] for k in ("columns", "event", "policy")],
            "refs": list(
                dict.fromkeys(f"{document_id}:p{proof[k]['page']}" for k in proof)
            ),
            "report_period": source.get("metadata", {}).get("period_end"),
            "published_at": source.get("metadata", {}).get("published_at"),
            "scope": "this_document_comparative_financial_statements_only",
            "limitation": "comparative changes are not all attributed to the merger; classification and other adjustments require separate explanation",
        }
    return None


def _disclosed_restatement_selection(ids: list[str], facts: dict) -> list[str]:
    """Select only an explicitly restated latest comparative, retaining all facts.

    A newer number alone is not sufficient. Same-date disagreements, currency,
    unit or scope mismatches remain unresolved conflicts.
    """
    meanings = {(facts[i].get("unit"), facts[i].get("scope")) for i in ids}
    known_currencies = {facts[i]["currency"] for i in ids if facts[i].get("currency")}
    if len(meanings) != 1 or len(known_currencies) > 1:
        return []
    latest = max(_day(facts[i]["published_at"]) for i in ids)
    candidates = [i for i in ids if _day(facts[i]["published_at"]) == latest]
    if len({Decimal(str(facts[i]["value"])) for i in candidates}) != 1:
        return []
    if known_currencies:
        candidates = [i for i in candidates if facts[i].get("currency")]
    for i in candidates:
        fact = facts[i]
        basis = fact.get("restatement_basis") or {}
        if (
            fact.get("column") == "comparative"
            and basis.get("method")
            in {
                "checked_affirmative_restatement_disclosure/v1",
                "reviewed_image_restatement_header/v1",
                "pdf_aligned_comparative_restatement_header/v1",
                "adjusted_comparatives_with_current_merger_evidence/v1",
            }
            and basis.get("quote")
            and basis.get("refs")
            and basis.get("reason")
            and basis.get("report_period")
            and fact["period"] < basis["report_period"]
            and basis.get("published_at") == fact.get("published_at")
        ):
            return candidates
    return []


def _observations(pack, source, document_id):
    fields = dict(pack["financial_fields"])
    fields["net_profit_total"] = extract_field(
        "net_profit_total",
        ("合并净利润", "income", "净利润"),
        pack["sections"],
        pack["metadata"],
    )
    meta = pack["metadata"]
    restatement = _restatement_disclosure(source, document_id)
    presentation_currency = _presentation_currency_disclosure(source, document_id)
    from .statement_restatement import comparative_header_basis

    for metric, field in fields.items():
        if field.get("status") != "obtained":
            continue
        pages = field.get("source_ref", {}).get("pages", [])
        original = "".join(source["pages"].get(str(p), "") for p in pages)
        # Preserve literal source validation rather than accepting arbitrary references.
        squash = lambda t: "".join(str(t).split())
        if (
            not pages
            or any(str(p) not in source["pages"] for p in pages)
            or squash(field.get("quote", "")) not in squash(original)
        ):
            continue
        currency_basis = presentation_currency if not field.get("currency") else None
        field_restatement = (
            comparative_header_basis(field, meta, document_id) or restatement
        )
        for column in ("current", "comparative"):
            key = "amount" if column == "current" else "comparative_amount"
            period_key = (
                "period_end" if column == "current" else "comparative_period_end"
            )
            if field.get(key) is None or not field.get(period_key):
                continue
            period = field[period_key]
            original_period = period
            if metric in STOCK_FIELDS and period.endswith("01-01"):
                period = (_day(period) - timedelta(days=1)).isoformat()
            restatement_basis = (
                field_restatement
                if column == "comparative"
                and field_restatement
                and period < meta["period_end"]
                else None
            )
            cash_sign_basis = (field.get("cash_payment_sign_basis") or {}).get(column)
            cash_sign_refs = [
                f"{document_id}:p{proof['page']}"
                for proof in (cash_sign_basis or {}).get("evidence", [])
                if str(proof.get("page")) in source["pages"]
                and squash(proof.get("quote", ""))
                and squash(proof["quote"])
                in squash(source["pages"][str(proof["page"])])
            ]
            yield {
                "issuer_id": meta["issuer_id"],
                "company_name": meta["company_name"],
                "metric": metric,
                "period": period,
                "original_column_period": original_period,
                "column": column,
                "kind": "FY"
                if period.endswith("12-31")
                else "H1"
                if period.endswith("06-30")
                else "YTD",
                "value": _number(field[key]),
                "raw_amount": str(field[key]),
                "raw_reported_amount": str(
                    field.get(
                        "reported_amount"
                        if column == "current"
                        else "comparative_reported_amount",
                        field[key],
                    )
                ),
                "sign_convention": field.get("sign_convention"),
                "cash_payment_sign_basis": cash_sign_basis,
                "restatement_basis": restatement_basis,
                "unit": "元",
                "currency": field.get("currency")
                or (currency_basis["currency"] if currency_basis else None),
                "original_currency": field.get("currency"),
                "currency_basis": currency_basis,
                "scope": "consolidated",
                "published_at": meta["published_at"],
                "document_id": document_id,
                "quote": field.get("quote", ""),
                "header": field.get("header", ""),
                "refs": list(
                    dict.fromkeys(
                        [f"{document_id}:p{p}" for p in pages]
                        + (restatement_basis["refs"] if restatement_basis else [])
                        + (currency_basis["refs"] if currency_basis else [])
                        + cash_sign_refs
                    )
                ),
            }


def build_comparison_panel(observations: list[dict], spec: dict) -> dict:
    """Build facts, safe per-period calculations and actual interval diagnostics."""
    as_of = _day(spec["as_of"])
    years = int(spec.get("history_years", 7))
    first_year = as_of.year - years
    companies = {
        x["issuer_id"]: x.get("name", x.get("company_name", x["issuer_id"]))
        for x in spec["companies"]
    }
    facts, grouped, issues = {}, {}, []
    for item in observations:
        if (
            item["issuer_id"] not in companies
            or _day(item["published_at"]) > as_of
            or _day(item["period"]) > as_of
        ):
            continue
        if int(item["period"][:4]) < first_year:
            continue
        fact = dict(item)
        ident = "F" + _hash(fact)[:24]
        facts[ident] = fact
        key = (fact["issuer_id"], fact["period"], fact["metric"])
        grouped.setdefault(key, []).append(ident)
    rows_by_key = {}
    for (issuer, period, metric), ids in sorted(grouped.items()):
        row = rows_by_key.setdefault(
            (issuer, period),
            {
                "issuer_id": issuer,
                "company_name": companies[issuer],
                "period": period,
                "kind": facts[ids[0]]["kind"],
                "values": {},
                "calculated": {},
                "refs": {},
                "currency": None,
                "warnings": [],
            },
        )
        values = {
            (
                Decimal(str(facts[i]["value"])),
                facts[i].get("currency"),
                facts[i].get("unit"),
                facts[i].get("scope"),
            )
            for i in ids
        }
        # A documented restatement is a new version, not an unexplained conflict.
        # Both originals remain in facts; only retrospective panel selection changes.
        active_ids = ids
        numeric_meanings = {
            (
                Decimal(str(facts[i]["value"])),
                facts[i].get("unit"),
                facts[i].get("scope"),
            )
            for i in ids
        }
        known_currencies = {
            facts[i]["currency"] for i in ids if facts[i].get("currency")
        }
        currency_completed = (
            len(values) > 1
            and len(numeric_meanings) == 1
            and len(known_currencies) == 1
        )
        if currency_completed:
            active_ids = [i for i in ids if facts[i].get("currency")]
            row["values"][metric] = facts[active_ids[0]]["value"]
            row.setdefault("version_history", {})[metric] = {
                "all_refs": ids,
                "selected_refs": active_ids,
                "superseded_refs": [i for i in ids if i not in active_ids],
                "policy": "identical_amount_with_explicit_currency_evidence",
            }
        elif len(values) != 1:
            restated = _disclosed_restatement_selection(ids, facts)
            if restated:
                active_ids = restated
                row["values"][metric] = facts[restated[0]]["value"]
                row.setdefault("version_history", {})[metric] = {
                    "all_refs": ids,
                    "selected_refs": restated,
                    "superseded_refs": [i for i in ids if i not in restated],
                    "policy": "documented_restatement_for_retrospective_only",
                }
                row["warnings"].append(
                    "disclosed_restatement_scope_change_not_organic_growth"
                )
                issues.append(
                    {
                        "code": "disclosed_restatement_applied",
                        "status": "resolved",
                        "issuer_id": issuer,
                        "period": period,
                        "metric": metric,
                        "refs": ids,
                        "selected_refs": restated,
                        "basis": [
                            facts[i]["restatement_basis"]
                            for i in restated
                            if facts[i].get("restatement_basis")
                        ],
                        "limitation": "original versions retained; no PIT backfill; scope changes may affect long-horizon comparisons",
                    }
                )
            else:
                row["values"][metric] = None
                issues.append(
                    {
                        "code": "conflicting_financial_versions",
                        "issuer_id": issuer,
                        "period": period,
                        "metric": metric,
                        "refs": ids,
                    }
                )
        else:
            row["values"][metric] = facts[ids[0]]["value"]
        row["refs"][metric] = active_ids
    calculated = {}
    for (issuer, period), row in sorted(rows_by_key.items()):
        values = row["values"]
        currencies = {
            facts[i].get("currency")
            for ids in row["refs"].values()
            for i in ids
            if facts[i].get("unit") not in {"股", "shares", "share", "万股", "亿股"}
        }
        row["currency"] = next(iter(currencies)) if len(currencies) == 1 else None
        if None in currencies:
            row["warnings"].append(
                "presentation_currency_not_explicit_no_fx_or_absolute_peer_ranking"
            )

        def calc(
            metric,
            value,
            expression,
            inputs,
            unit="ratio",
            extra_refs=(),
            row=row,
            issuer=issuer,
            period=period,
        ):
            refs = sorted(
                {i for k in inputs for i in row["refs"].get(k, [])} | set(extra_refs)
            )
            row["calculated"][metric] = value
            ident = "C" + _hash([issuer, period, metric, refs, expression])[:24]
            calculated[ident] = {
                "value": value,
                "metric": metric,
                "issuer_id": issuer,
                "period": period,
                "currency": row["currency"],
                "unit": unit,
                "expression": expression,
                "refs": refs,
            }

        def coherent(inputs, row=row, values=values):
            if any(values.get(k) is None for k in inputs):
                return False
            cc = {
                facts[i].get("currency")
                for k in inputs
                for i in row["refs"].get(k, [])
                if facts[i].get("unit") not in {"股", "shares", "share", "万股", "亿股"}
            }
            return len(cc) == 1

        if coherent(["cfo", "capex"]):
            proxy = float(Decimal(str(values["cfo"])) - Decimal(str(values["capex"])))
        else:
            proxy = None
        calc(
            "fcf_proxy",
            proxy,
            "cfo - capex; basic proxy, not normalized shareholder FCF",
            ["cfo", "capex"],
            "元",
        )
        for metric, num, den in [
            ("gross_margin", "cost", "revenue"),
            ("cfo_conversion", "cfo", "net_profit_total"),
            ("net_margin_consolidated", "net_profit_total", "revenue"),
        ]:
            result = (
                _ratio(values.get(num), values.get(den))
                if coherent([num, den])
                else None
            )
            if metric == "gross_margin" and result is not None:
                result = 1 - result
            calc(
                metric,
                result,
                f"{'1 - ' if metric == 'gross_margin' else ''}{num} / {den}",
                [num, den],
            )
        # Industry-neutral exposure anchors. These are not operating cycle
        # estimates: balances divided by annual flows cannot establish duration.
        for anchor, numerator, denominator in [
            ("inventory_to_annual_cost", "inventory", "cost"),
            ("contract_assets_to_annual_revenue", "contract_assets", "revenue"),
            ("receivables_to_annual_revenue", "receivables", "revenue"),
            ("interest_to_operating_profit", "interest_expense", "operating_profit"),
        ]:
            value = (
                _ratio(values.get(numerator), values.get(denominator))
                if row["kind"] == "FY" and coherent([numerator, denominator])
                else None
            )
            calc(
                anchor,
                value,
                f"{numerator} / {denominator}; FY exposure ratio, not cash collection duration",
                [numerator, denominator],
            )
        previous = (
            rows_by_key.get((issuer, f"{int(period[:4]) - 1}-12-31"), {})
            if row["kind"] == "FY"
            else {}
        )
        prev = previous.get("values", {}).get("parent_equity")
        end = values.get("parent_equity")
        avg = (prev + end) / 2 if prev is not None and end is not None else None
        equity_refs = previous.get("refs", {}).get("parent_equity", [])
        equity_cc = {
            facts[i].get("currency")
            for i in equity_refs
            + row["refs"].get("parent_equity", [])
            + row["refs"].get("net_profit_parent", [])
        }
        roe = (
            _ratio(values.get("net_profit_parent"), avg)
            if len(equity_cc) == 1
            else None
        )
        calc(
            "simple_roe",
            roe,
            "net_profit_parent / mean(opening_parent_equity, closing_parent_equity); FY only",
            ["net_profit_parent", "parent_equity"],
            extra_refs=equity_refs,
        )
        # share_capital is not an acceptable substitute for disclosed share count.
        bvps = _ratio(values.get("parent_equity"), values.get("shares_outstanding"))
        calc(
            "bvps",
            bvps,
            "parent_equity / actual_closing_issued_ordinary_shares; parent equity may include other equity instruments; denominator includes uncancelled treasury shares",
            ["parent_equity", "shares_outstanding"],
            "元/股",
        )
        common_bvps = (
            _ratio(
                values["parent_equity"] - values["other_equity"],
                values["shares_outstanding"],
            )
            if coherent(["parent_equity", "other_equity", "shares_outstanding"])
            else None
        )
        calc(
            "common_equity_bvps",
            common_bvps,
            "(parent_equity - explicitly_disclosed_other_equity) / actual_closing_issued_ordinary_shares; no undisclosed-zero assumption; denominator includes uncancelled treasury shares",
            ["parent_equity", "other_equity", "shares_outstanding"],
            "元/股",
        )
    rows = list(rows_by_key.values())
    metrics, coverage = {}, []
    for issuer, name in companies.items():
        annual = sorted(
            [r for r in rows if r["issuer_id"] == issuer and r["kind"] == "FY"],
            key=lambda x: x["period"],
        )
        growth, rolling = {}, []
        for metric in CORE_GROWTH:
            points = [
                (
                    int(r["period"][:4]),
                    r["values"].get(metric)
                    if metric != "fcf_proxy"
                    else r["calculated"].get(metric),
                )
                for r in annual
            ]
            valid = [(y, v) for y, v in points if v is not None]
            if len(valid) >= 2:
                a, b = valid[0], valid[-1]
                growth[metric] = {
                    "value": _growth(a[1], b[1], b[0] - a[0]),
                    "start_year": a[0],
                    "end_year": b[0],
                    "intervals": b[0] - a[0],
                    "observed_points": len(valid),
                    "missing_years": sorted(
                        set(range(a[0], b[0] + 1)) - {p[0] for p in valid}
                    ),
                }
            for y, val in valid:
                start = dict(valid).get(y - 3)
                if start is not None:
                    rolling.append(
                        {
                            "metric": metric,
                            "start_year": y - 3,
                            "end_year": y,
                            "intervals": 3,
                            "value": _growth(start, val, 3),
                        }
                    )
        paired = [
            r
            for r in annual
            if r["values"].get("cfo") is not None
            and r["values"].get("net_profit_total") is not None
        ]

        def cumulative_block(block):
            block_refs = sorted(
                {
                    i
                    for r in block
                    for k in ("cfo", "net_profit_total")
                    for i in r["refs"].get(k, [])
                }
            )
            currencies = {facts[i].get("currency") for i in block_refs}
            coherent = bool(block) and len(currencies) == 1
            block_cfo = sum(r["values"]["cfo"] for r in block) if coherent else None
            block_profit = (
                sum(r["values"]["net_profit_total"] for r in block)
                if coherent
                else None
            )
            currency = next(iter(currencies)) if coherent else None
            return {
                "value": _ratio(block_cfo, block_profit) if coherent else None,
                "cfo_sum": block_cfo,
                "net_profit_total_sum": block_profit,
                "periods": [r["period"] for r in block],
                "currency": currency,
                "currency_status": "confirmed"
                if currency
                else "presentation_currency_unconfirmed"
                if coherent
                else "incompatible_or_missing_period_currency",
                "refs": block_refs,
                "expression": "sum(consolidated CFO matched periods) / sum(consolidated net profit matched periods)",
            }

        conversion = cumulative_block(paired)
        segments, active, active_currency = [], [], object()
        for row in paired:
            row_currencies = {
                facts[i].get("currency")
                for k in ("cfo", "net_profit_total")
                for i in row["refs"].get(k, [])
            }
            if len(row_currencies) != 1:
                if active:
                    segments.append(cumulative_block(active))
                    active = []
                continue
            currency = next(iter(row_currencies))
            if active and (
                currency != active_currency
                or int(row["period"][:4]) != int(active[-1]["period"][:4]) + 1
            ):
                segments.append(cumulative_block(active))
                active = []
            active.append(row)
            active_currency = currency
        if active:
            segments.append(cumulative_block(active))
        metrics[issuer] = {
            "growth": growth,
            "rolling_three_year": rolling,
            "cumulative_cash_conversion": conversion,
            "cumulative_cash_conversion_segments": segments,
        }
        coverage.append(
            {
                "issuer_id": issuer,
                "company_name": name,
                "requested_complete_years": list(range(first_year, as_of.year)),
                "actual_annual_years": [int(r["period"][:4]) for r in annual],
                "missing_annual_years": sorted(
                    set(range(first_year, as_of.year))
                    - {int(r["period"][:4]) for r in annual}
                ),
                "interim_periods": [
                    r["period"]
                    for r in rows
                    if r["issuer_id"] == issuer and r["kind"] != "FY"
                ],
            }
        )
    return {
        "facts": facts,
        "rows": rows,
        "calculated_values": calculated,
        "metrics": metrics,
        "coverage": coverage,
        "issues": issues,
    }


def historical_month_end_valuations(
    prices: list[dict], statements: list[dict], *, as_of: str | None = None
) -> dict:
    """Month-end available trading observation, original shares, no forward data.

    Prices: issuer_id,date,close,adjustment='none',shares_outstanding,currency,
    refs. Statements: issuer_id,period_end,published_at,currency,refs plus
    parent_equity and optional explicitly computed net_profit_ttm/fcf_proxy_ttm.
    Reporting dates alone do not establish publication timestamps. Restatements
    are versioned events, selected only after their own publication date.
    """
    eligible, issues = {}, []
    for price in prices:
        day = _day(price["date"])
        if as_of and day > _day(as_of):
            continue
        if (
            price.get("adjustment") != "none"
            or not price.get("refs")
            or not price.get("currency")
        ):
            issues.append(
                {
                    "code": "unverified_or_adjusted_price",
                    "issuer_id": price.get("issuer_id"),
                    "date": price["date"],
                }
            )
            continue
        key = (price["issuer_id"], day.year, day.month)
        if key not in eligible or day > _day(eligible[key]["date"]):
            eligible[key] = price
    rows = []
    for (issuer, year, month), price in sorted(eligible.items()):
        day = _day(price["date"])
        available = [
            s
            for s in statements
            if s.get("issuer_id") == issuer
            and s.get("published_at")
            and _day(s["published_at"]) < day
            and _day(s["period_end"]) <= day
            and s.get("refs")
            and s.get("currency") == price["currency"]
        ]
        statement = (
            max(available, key=lambda s: (s["period_end"], s["published_at"]))
            if available
            else {}
        )
        shares = _number(price.get("shares_outstanding"))
        close = _number(price.get("close"))
        cap = (
            shares * close
            if shares is not None and shares > 0 and close is not None and close > 0
            else None
        )
        row = {
            "issuer_id": issuer,
            "month_end": date(year, month, monthrange(year, month)[1]).isoformat(),
            "price_date": price["date"],
            "currency": price["currency"],
            "market_cap": cap,
            "statement_period": statement.get("period_end"),
            "published_at": statement.get("published_at"),
            "pe": _ratio(cap, _number(statement.get("net_profit_ttm"))),
            "pb": _ratio(cap, _number(statement.get("parent_equity"))),
            "fcf_yield": _ratio(_number(statement.get("fcf_proxy_ttm")), cap),
            "refs": price["refs"] + statement.get("refs", []),
            "status": "available" if cap is not None and statement else "insufficient",
            "limits": [],
        }
        if day.day != monthrange(year, month)[1]:
            row["limits"].append(
                "last_observed_trading_date; calendar_completeness_not_certified"
            )
        if not statement:
            row["limits"].append("no_prior_disclosed_comparable_statement")
        if cap is None:
            row["limits"].append("historical_shares_or_positive_price_missing")
        rows.append(row)
    if not prices:
        issues.append({"code": "historical_price_inputs_missing"})
    if not statements:
        issues.append({"code": "pit_statement_inputs_missing"})
    return {
        "rows": rows,
        "issues": issues,
        "method": "last_observed_monthly_unadjusted_price; strict_prior_disclosure; original_version_events",
    }


def prepare_comparison(
    spec: dict,
    pack_roots=(),
    source_paths=(),
    output: Path | None = None,
    *,
    document_specs=(),
    price_events=(),
    statement_events=(),
    external_evidence=None,
    supplemental_facts_paths=(),
    image_evidence_paths=(),
) -> dict:
    """Freeze reusable FR-backed inputs. Does not call models or providers."""
    companies = {c["issuer_id"] for c in spec["companies"]}
    loaded, issues = [], []
    for root in map(Path, pack_roots):
        for entry in read_json(root / "latest_run.json")["entries"]:
            if not entry.get("pack_key"):
                issues.append({"code": "pack_unavailable", "entry": entry})
                continue
            try:
                pack, source = verified_pack(root / "packs" / entry["pack_key"])
                if pack["metadata"]["issuer_id"] in companies:
                    meta = pack["metadata"]
                    if (
                        hashlib.sha256(Path(meta["path"]).read_bytes()).hexdigest()
                        != meta["sha256"]
                    ):
                        raise ValueError("original_pdf_changed")
                    loaded.append((pack, source))
            except (ValueError, OSError, KeyError) as exc:
                issues.append(
                    {
                        "code": "pack_integrity_failed",
                        "pack_key": entry["pack_key"],
                        "error": str(exc),
                    }
                )
    for path in map(Path, source_paths):
        source = read_json(path)
        if source["metadata"]["issuer_id"] in companies:
            meta = source["metadata"]
            pdf = Path(meta["path"])
            if (
                not pdf.is_file()
                or hashlib.sha256(pdf.read_bytes()).hexdigest() != meta["sha256"]
            ):
                issues.append({"code": "pdf_source_hash_mismatch", "source": str(path)})
                continue
            source_errors = [
                e
                for e in validate_source(source)
                if e != "empty_page_text_requires_visual_retrieval"
            ]
            if source_errors:
                issues.append(
                    {
                        "code": "source_validation_failed",
                        "source": str(path),
                        "errors": source_errors,
                    }
                )
                continue
            loaded.append((make_pack(source, _hash([source, PARSER_VERSION])), source))
    for meta in document_specs:
        if meta["issuer_id"] not in companies:
            continue
        try:
            source = source_from_pdf(meta)
            loaded.append((make_pack(source, _hash([source, PARSER_VERSION])), source))
        except (ValueError, OSError) as exc:
            issues.append(
                {
                    "code": "pdf_preparation_failed",
                    "issuer_id": meta["issuer_id"],
                    "source": meta["path"],
                    "error": str(exc),
                }
            )
    documents, chapters, observations, seen = [], [], [], set()
    for pack, source in loaded:
        meta = pack["metadata"]
        if _day(meta["published_at"]) > _day(spec["as_of"]):
            continue
        document_id = str(meta["document_id"]) + "@" + meta["sha256"][:12]
        if document_id in seen:
            continue
        seen.add(document_id)
        gate = assess_materials(pack["financial_fields"], meta, source)
        documents.append(
            {
                "document_id": document_id,
                "issuer_id": meta["issuer_id"],
                "company_name": meta["company_name"],
                "period_end": meta["period_end"],
                "published_at": meta["published_at"],
                "source_path": meta["path"],
                "source_sha256": meta["sha256"],
                "source_url": meta.get("url", ""),
                "pages": source["pages"],
                "gate": gate,
                "parser_version": pack.get("parser_version"),
                "sparse_pages": [
                    int(p) for p, text in source["pages"].items() if not text.strip()
                ],
                "accounting_topics": pack.get("accounting_topics", {}),
                "management_discussion": pack.get("management_discussion", {}),
            }
        )
        for section in pack["sections"]:
            chapters.append(
                {
                    "id": f"{document_id}:{section['id']}",
                    "document_id": document_id,
                    "issuer_id": meta["issuer_id"],
                    "title": section["title"],
                    "kind": section["kind"],
                    "pages": section["pages"],
                    "text": section["text"],
                    "truncated": False,
                }
            )
        observations.extend(_observations(pack, source, document_id))
    supplements = _reuse_supplements(supplemental_facts_paths, documents, loaded, spec)
    observations.extend(supplements["observations"])
    from .share_count_sources import extract_share_count_observations

    shares = extract_share_count_observations(documents)
    observations.extend(shares["observations"])
    for issue in shares["issues"]:
        matching = [
            item
            for item in observations
            if item.get("metric") == "shares_outstanding"
            and item.get("issuer_id") == issue.get("issuer_id")
            and item.get("period") == issue.get("period")
        ]
        if (
            issue.get("code") == "closing_share_count_not_located"
            and matching
            and len({item["value"] for item in matching}) == 1
        ):
            issue = {
                **issue,
                "status": "resolved",
                "resolution": "verified_existing_share_observation",
                "resolution_refs": sorted(
                    {ref for item in matching for ref in item.get("refs", [])}
                ),
            }
        issues.append(issue)
    source_versions = {"share_count": shares["version"]}
    merged_external = dict(external_evidence or {})
    if image_evidence_paths:
        from .fr_image_evidence import load_reviewed_image_facts

        for manifest in image_evidence_paths:
            image_data = load_reviewed_image_facts(Path(manifest), documents)
            observations.extend(image_data["observations"])
            issues.extend(image_data["issues"])
            for key, value in image_data["external_evidence"].items():
                if key in merged_external and merged_external[key] != value:
                    raise ValueError("external_evidence_ref_conflict:" + key)
                merged_external[key] = value
            source_versions["image_evidence"] = image_data["version"]
    panel = build_comparison_panel(observations, spec)
    panel["facts"].update(supplements["facts"])
    panel["calculated_values"].update(supplements["calculated_values"])
    issues.extend(supplements["issues"])
    packet = {
        "schema_version": VERSION,
        "spec": spec,
        "documents": documents,
        "chapters": chapters,
        "reading_catalog": _reading_catalog(documents, chapters, spec),
        "external_evidence": merged_external,
        "source_method_versions": source_versions,
        "observations": observations,
        "cost_anchors": supplements["cost_anchors"],
        **panel,
        "historical_valuations": historical_month_end_valuations(
            list(price_events), list(statement_events), as_of=spec["as_of"]
        ),
        "external_requests": 0,
        "model_calls": 0,
        "research_acceptance": "not_research_report",
        "limits": [
            "structural_extraction_not_accounting_interpretation",
            "missing_currency_prevents_absolute_peer_ranking",
            "share_capital_not_share_count",
        ],
    }
    packet["issues"] += issues
    packet["packet_hash"] = _hash(packet)
    if output is not None:
        write_json(Path(output), packet)
    return packet


def load_cached_valuation_events(
    spec: dict, root: Path = Path("data/point_in_time")
) -> dict:
    """Read existing raw-and-qfq-1 caches and original statement revisions only.

    Missing daily share histories are explicit: a later/latest statement share
    count is never projected backwards onto a historical price. No providers run.
    """
    import csv

    from ..instruments.point_in_time import PointInTimeFundamentalStore

    root = Path(spec.get("valuation_store_root", root))
    store = PointInTimeFundamentalStore(root)
    prices, statements, issues = [], [], []
    external_evidence = {}
    cutoff = _day(spec["as_of"])
    start = date(cutoff.year - int(spec.get("history_years", 7)), 1, 1)
    for company in spec["companies"]:
        issuer = company["issuer_id"]
        code = issuer.split(":")[-1]
        statement_path = root / "fundamentals" / f"{code}.statements.json"
        snapshots = store.read_all(code)
        statement_file_hash = (
            hashlib.sha256(statement_path.read_bytes()).hexdigest()
            if statement_path.exists()
            else None
        )
        versions = []
        for item in snapshots:
            data = item.dict()
            if not data.get("published_at") or data["published_at"] > cutoff:
                continue
            data["_ref"] = (
                f"{statement_path.resolve()}#{_hash({k: str(v) for k, v in data.items()})}"
            )
            external_evidence[data["_ref"]] = {
                "source_type": "point_in_time_statement_cache",
                "path": str(statement_path.resolve()),
                "file_sha256": statement_file_hash,
                "record": {
                    k: v.isoformat() if isinstance(v, date) else v
                    for k, v in data.items()
                    if k != "_ref"
                },
                "validation": "read_by_existing_PointInTimeFundamentalStore; versioned_disclosure_not_pdf_fact",
                "issuer_id": issuer,
            }
            versions.append(data)
        for published in sorted({s["published_at"] for s in versions}):
            available = [s for s in versions if s["published_at"] <= published]

            # Never pick one of conflicting same-period values arbitrarily.
            def value_at(
                period, field, available=available, issuer=issuer, published=published
            ):
                candidates = [
                    s
                    for s in available
                    if s["period_end"] == period and s.get(field) is not None
                ]
                if not candidates:
                    return None, []
                latest = max(s["published_at"] for s in candidates)
                candidates = [s for s in candidates if s["published_at"] == latest]
                values = {s[field] for s in candidates}
                currencies = {s.get("currency") for s in candidates}
                if len(values) != 1 or len(currencies) != 1:
                    issues.append(
                        {
                            "code": "pit_statement_conflict",
                            "issuer_id": issuer,
                            "period_end": period.isoformat(),
                            "field": field,
                            "published_at": published.isoformat(),
                        }
                    )
                    return None, []
                return candidates[0][field], [s["_ref"] for s in candidates]

            latest_period = max(s["period_end"] for s in available)
            newest = [s for s in available if s["period_end"] == latest_period]
            newest_currency = {s.get("currency") for s in newest}
            currency = (
                next(iter(newest_currency)) if len(newest_currency) == 1 else None
            )
            refs = []

            def ttm(
                field,
                latest_period=latest_period,
                available=available,
                refs=refs,
                value_at=value_at,
            ):
                current, current_refs = value_at(latest_period, field)
                if current is None:
                    return None
                current_versions = [
                    s
                    for s in available
                    if s["period_end"] == latest_period and s.get(field) is not None
                ]
                if not all(
                    s.get("is_cumulative") or s.get("period_type") == "year"
                    for s in current_versions
                ):
                    return None
                refs.extend(current_refs)
                if latest_period.month == 12:
                    return current
                prior_annual, annual_refs = value_at(
                    date(latest_period.year - 1, 12, 31), field
                )
                prior_period, prior_refs = value_at(
                    latest_period.replace(year=latest_period.year - 1), field
                )
                refs.extend(annual_refs + prior_refs)
                leg_refs = set(current_refs + annual_refs + prior_refs)
                if (
                    len({s.get("currency") for s in available if s["_ref"] in leg_refs})
                    != 1
                ):
                    return None
                return (
                    current + prior_annual - prior_period
                    if prior_annual is not None and prior_period is not None
                    else None
                )

            profit = ttm("net_income_parent")
            cfo, capex = ttm("operating_cash_flow"), ttm("capital_expenditures")
            equity, equity_refs = value_at(latest_period, "parent_equity")
            refs.extend(equity_refs)
            if refs:
                statements.append(
                    {
                        "issuer_id": issuer,
                        "period_end": latest_period.isoformat(),
                        "published_at": published.isoformat(),
                        "currency": currency,
                        "parent_equity": equity,
                        "net_profit_ttm": profit,
                        "fcf_proxy_ttm": cfo - capex
                        if cfo is not None and capex is not None
                        else None,
                        "refs": sorted(set(refs)),
                        "source": "local_point_in_time_store",
                        "ttm_method": "annual or current_YTD + previous_FY - previous_YTD, using versions available at disclosure",
                    }
                )
        if not snapshots:
            issues.append(
                {
                    "code": "pit_fundamentals_cache_missing",
                    "issuer_id": issuer,
                    "path": str(statement_path),
                }
            )
        csv_path = root / "market" / f"{code}.csv"
        meta_path = root / "market" / f"{code}.meta.json"
        if not csv_path.exists() or not meta_path.exists():
            issues.append(
                {
                    "code": "raw_price_cache_missing",
                    "issuer_id": issuer,
                    "path": str(csv_path),
                }
            )
            continue
        meta = read_json(meta_path)
        if meta.get("contract") != "raw-and-qfq-1" or not meta.get("currency"):
            issues.append(
                {
                    "code": "raw_price_contract_or_currency_unverified",
                    "issuer_id": issuer,
                }
            )
            continue
        csv_file_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = _day(row["date"])
                if (
                    day < start
                    or day > cutoff
                    or str(row.get("tradable", "true")).lower() == "false"
                ):
                    continue
                close = row.get("raw_close")
                if not close:
                    continue
                shares = row.get("shares_outstanding")
                price_ref = f"{csv_path.resolve()}#{day.isoformat()}:raw_close"
                external_evidence[price_ref] = {
                    "source_type": "raw_market_cache",
                    "path": str(csv_path.resolve()),
                    "file_sha256": csv_file_hash,
                    "issuer_id": issuer,
                    "record": dict(row),
                    "metadata": meta,
                    "validation": "raw-and-qfq-1 raw_close only; historical daily shares only when present",
                }
                prices.append(
                    {
                        "issuer_id": issuer,
                        "date": day.isoformat(),
                        "close": _number(close),
                        "adjustment": "none",
                        "currency": meta["currency"],
                        "shares_outstanding": _number(shares) if shares else None,
                        "refs": [price_ref],
                    }
                )
        if any(
            p["issuer_id"] == issuer and p["shares_outstanding"] is None for p in prices
        ):
            issues.append(
                {
                    "code": "daily_historical_share_count_missing_no_projection_from_latest_report",
                    "issuer_id": issuer,
                }
            )
    return {
        "price_events": prices,
        "statement_events": statements,
        "external_evidence": external_evidence,
        "issues": issues,
        "external_requests": 0,
    }


def _reading_catalog(documents: list, chapters: list, spec: dict) -> dict:
    """Compact discovery index; full source content remains in the tool store.

    Cost-anchor candidates are source sections, not extracted/verified unit costs.
    No prior human-authored company conclusions or numeric anchors are injected.
    """
    industry = spec.get("industry", spec.get("industry_method", "generic"))
    profiles = {
        "liquor": [
            "product_revenue_volume_cost",
            "channel_mix",
            "inventory_age_and_costing",
        ],
        "construction": [
            "project_margin",
            "contract_assets_and_collection",
            "impairment_and_financing",
        ],
        "generic": ["segment_revenue_cost", "working_capital", "financing"],
    }
    profile = (
        "liquor"
        if industry in {"liquor", "white_liquor", "白酒"}
        else "construction"
        if industry in {"construction", "engineering", "工程建设"}
        else "generic"
    )
    result = []
    for document in documents:
        ident = document["document_id"]
        sections = [c for c in chapters if c["document_id"] == ident]
        notes = {}
        for topic, value in document.get("accounting_topics", {}).items():
            notes[topic] = {
                "status": value.get("status"),
                "policy": [f"{ident}:{i}" for i in value.get("policy_sections", [])],
                "amount_notes": [
                    f"{ident}:{i}" for i in value.get("note_sections", [])
                ],
                "management": [
                    f"{ident}:{i}" for i in value.get("management_sections", [])
                ],
            }
        result.append(
            {
                "document_id": ident,
                "issuer_id": document["issuer_id"],
                "period_end": document["period_end"],
                "full_mda_chapters": [c["id"] for c in sections if c["kind"] == "mda"],
                "financial_statements": [
                    c["id"]
                    for c in sections
                    if c["kind"] in {"balance", "income", "cashflow"}
                ],
                "accounting_topics": notes,
                "cost_anchor_candidates": [
                    c["id"]
                    for c in sections
                    if any(
                        word in c["title"]
                        for word in (
                            "营业收入",
                            "营业成本",
                            "存货",
                            "合同资产",
                            "分部",
                            "财务费用",
                        )
                    )
                    and c["kind"] not in {"chapter", "mda"}
                ],
                "candidate_status": "source_sections_only_requires_research_and_independent_review",
                "all_content_available": True,
            }
        )
    return {
        "industry": industry,
        "method_profile": profile,
        "anchor_questions": profiles[profile],
        "documents": result,
        "context_policy": "send compact catalog first; tools return full requested chapters without duplicating all nested sections in every request",
    }


def _reuse_supplements(paths, documents, loaded, spec):
    """Reuse reviewed numbers only after matching original hash/page/literal.

    No old analyst narrative or investment conclusion is accepted here. Later
    comparative numbers retain their true later disclosure date, never PIT backfill.
    """
    import re

    available = {d["source_sha256"]: d for d in documents}
    aliases = {
        (source["metadata"]["short_id"], source["metadata"]["sha256"]): source
        for _, source in loaded
    }
    result = {
        "observations": [],
        "facts": {},
        "calculated_values": {},
        "cost_anchors": [],
        "issues": [],
    }
    cutoff = _day(spec["as_of"])

    def verified(item):
        sha = item.get("source_pdf_sha256")
        document = available.get(sha)
        source = aliases.get((item.get("document_id"), sha))
        if not document or not source:
            raise ValueError("supplement_original_source_unavailable")
        if (
            item.get("issuer_id") != document["issuer_id"]
            or item.get("published_at") != document["published_at"]
        ):
            raise ValueError("supplement_identity_or_disclosure_mismatch")
        page = str(item["page"])
        text = source["pages"].get(page, "")
        quote = item.get("quote", "")
        if not quote or "".join(quote.split()) not in "".join(text.split()):
            raise ValueError("supplement_quote_not_literal_in_original_page")
        raw = str(item.get("raw_value", item["value"]))
        value = Decimal(str(item["value"]).replace(",", ""))
        numeric_tokens = re.findall(
            r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])", quote.replace(",", "")
        )
        wrapped_literal = re.search(
            r"(?<![0-9.,])" + r"\s*".join(re.escape(c) for c in raw) + r"(?![0-9.,])",
            quote,
        )
        column_reconstruction = None
        if item["metric"] in {"segment_revenue", "segment_cost"}:
            lines = quote.splitlines()
            for offset in range(len(lines) - 2):
                heads = re.fullmatch(r"([0-9,]+)\s+([0-9,]+)", lines[offset].strip())
                tails = re.fullmatch(
                    r"([0-9]+\.[0-9]{2})\s+([0-9]+\.[0-9]{2})",
                    lines[offset + 2].strip(),
                )
                if heads and tails and str(item.get("scope", "")) in lines[offset + 1]:
                    column = 1 if item["metric"] == "segment_revenue" else 2
                    joined = heads[column] + tails[column]
                    if Decimal(joined.replace(",", "")) == value:
                        column_reconstruction = {
                            "method": "two_columns_same_column_head_tail",
                            "column": column,
                            "head": heads[column],
                            "tail": tails[column],
                            "joined": joined,
                            "row_label": lines[offset + 1],
                        }
        if (
            not value.is_finite()
            or (
                value not in {Decimal(x) for x in numeric_tokens}
                and not wrapped_literal
                and not column_reconstruction
            )
            or Decimal(raw.replace(",", "")) != value
        ):
            raise ValueError("supplement_value_not_literal_in_quote")
        period = item["period_end"]
        if _day(period) > cutoff or _day(item["published_at"]) > cutoff:
            raise ValueError("supplement_after_research_cutoff")
        metric = {
            "parent_net_profit": "net_profit_parent",
            "shares_outstanding_period_end": "shares_outstanding",
        }.get(item["metric"], item["metric"])
        return {
            "issuer_id": item["issuer_id"],
            "company_name": item.get("company_name", document["company_name"]),
            "metric": metric,
            "period": period,
            "kind": "FY"
            if period.endswith("12-31")
            else "H1"
            if period.endswith("06-30")
            else "YTD",
            "value": float(value),
            "raw_amount": str(value),
            "unit": item["unit"],
            "currency": item.get("currency"),
            "scope": item.get("scope"),
            "published_at": item["published_at"],
            "document_id": document["document_id"],
            "quote": quote,
            "refs": [f"{document['document_id']}:p{page}"],
            "origin": "reused_prior_reviewed_source_fact",
            "source_numeric_reconstruction": column_reconstruction,
            "restated": item.get("restated", False),
            "evidence_type": item.get("evidence_type", "disclosed_amount"),
        }

    def add_fact(fact):
        ident = "F" + _hash(fact)[:24]
        result["facts"][ident] = fact
        return ident

    for path in map(Path, paths):
        supplement = read_json(path)
        for item in supplement.get("history_2021", []) + supplement.get(
            "shares_2025", []
        ):
            try:
                fact = verified(item)
                result["observations"].append(fact)
            except (ValueError, KeyError) as exc:
                result["issues"].append(
                    {
                        "code": "supplement_rejected",
                        "path": str(path),
                        "metric": item.get("metric"),
                        "reason": str(exc),
                    }
                )
        for entry in supplement.get("cost_anchors_2025", []):
            try:
                fields = [verified(f) for f in entry["fields"]]
                if (
                    len({(f["issuer_id"], f["period"], f["scope"]) for f in fields})
                    != 1
                ):
                    raise ValueError("cost_anchor_scope_mismatch")
                fields_by_metric = {f["metric"]: f for f in fields}
                revenue, cost, volume = [
                    fields_by_metric[k]
                    for k in ("segment_revenue", "segment_cost", "sales_volume")
                ]
                if (
                    revenue["unit"] != "元"
                    or cost["unit"] != "元"
                    or volume["unit"] != "吨"
                    or revenue["currency"] != cost["currency"]
                ):
                    raise ValueError("cost_anchor_unit_or_currency_mismatch")
                refs = [add_fact(f) for f in fields]
                values = {
                    "revenue_per_tonne": _ratio(revenue["value"], volume["value"]),
                    "cost_per_tonne": _ratio(cost["value"], volume["value"]),
                }
                result["cost_anchors"].append(
                    {
                        "issuer_id": revenue["issuer_id"],
                        "company_name": revenue["company_name"],
                        "period": revenue["period"],
                        "scope": revenue["scope"],
                        "currency": revenue["currency"],
                        "values": values,
                        "refs": refs,
                        "comparability_limit": "Product mix, ageing and sales-volume scope must be reviewed before ranking; not a pure price or production-cost measure.",
                    }
                )
                for metric, value in values.items():
                    ident = (
                        "C"
                        + _hash(
                            [
                                entry["issuer_id"],
                                revenue["period"],
                                revenue["scope"],
                                metric,
                                refs,
                            ]
                        )[:24]
                    )
                    result["calculated_values"][ident] = {
                        "issuer_id": revenue["issuer_id"],
                        "period": revenue["period"],
                        "metric": metric,
                        "scope": revenue["scope"],
                        "value": value,
                        "unit": "元/吨",
                        "currency": revenue["currency"],
                        "refs": refs,
                        "expression": "segment_revenue / sales_volume"
                        if metric == "revenue_per_tonne"
                        else "segment_cost / sales_volume",
                    }
            except (ValueError, KeyError) as exc:
                result["issues"].append(
                    {
                        "code": "cost_anchor_rejected",
                        "issuer_id": entry.get("issuer_id"),
                        "scope": entry.get("scope"),
                        "reason": str(exc),
                    }
                )
    return result
