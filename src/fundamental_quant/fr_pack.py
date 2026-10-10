"""Offline FR packs: immutable evidence, structural extraction, explicit gaps.

This module has no network or LLM dependency. Finding text is not semantic review.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .fr_pack_contract import FIELDS, PARSER_VERSION, SUPPLEMENTS, TOPICS

MONEY = re.compile(
    r"(?<![\d.,])[-−－—]?(?:\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})(?![\d.,])"
)
CASH_OUTFLOW_FIELDS = {
    "capex",
    "investment_cash_out",
    "acquisition_cash",
    "debt_repayment_cash",
    "distribution_interest_cash",
    "tax_cash",
}
CHAPTER = re.compile(r"^第[一二三四五六七八九十百\d]+节\s*[^.。…]{2,40}$")
MAJOR = re.compile(r"^[一二三四五六七八九十]+、\s*(.+)$")
NUMBERED = re.compile(r"^\d+(?:[、．]|\.(?=\s|[\u4e00-\u9fff]))\s*(.{1,70})$")
STATEMENT = re.compile(
    r"^(?:\d+[、．]\s*)?(合并|母公司|公司)?(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)$"
)
BLOCK_NAMES = (
    "财务报表的编制基础",
    "重要会计政策",
    "税项",
    "合并财务报表项目",
    "合并财务报告项目",
    "合并范围",
    "在其他主体",
    "金融工具",
    "公允价值",
    "关联方",
    "股份支付",
    "承诺及或有",
    "资产负债表日后",
    "其他重要事项",
    "母公司财务报表",
    "补充资料",
    "公司基本情况",
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def records(pages: dict) -> list:
    return [
        (int(p), i + 1, line)
        for p, text in sorted(pages.items(), key=lambda x: int(x[0]))
        for i, line in enumerate(text.splitlines())
    ]


def span(rows: list, start: int, end: int, title: str, kind: str) -> dict:
    selected = rows[start:end]
    return {
        "id": f"S{start}_{end}",
        "title": title,
        "kind": kind,
        "start": start,
        "end": end,
        "pages": sorted({x[0] for x in selected}),
        "lines": [{"page": p, "line": n, "text": t} for p, n, t in selected],
        "text": "\n".join(x[2] for x in selected),
        "boundary_check": "structural_heading_to_next_peer_heading",
    }


def locate_sections(pages: dict) -> list:
    rows = records(pages)
    result = []
    chapters = [
        (i, t.strip())
        for i, (_, _, t) in enumerate(rows)
        if CHAPTER.fullmatch(t.strip())
    ]
    for k, (i, title) in enumerate(chapters):
        end = chapters[k + 1][0] if k + 1 < len(chapters) else len(rows)
        kind = "mda" if "管理层讨论" in title or "董事会报告" in title else "chapter"
        result.append(span(rows, i, end, title, kind))
    heads = []
    for i, (_, _, t) in enumerate(rows):
        m = MAJOR.match(t.strip())
        if m and any(m[1].startswith(x) for x in BLOCK_NAMES):
            heads.append((i, m[1]))
    for k, (i, title) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(rows)
        kind = (
            "policy"
            if "重要会计政策" in title
            else "notes"
            if "合并财务" in title
            else "other_notes"
        )
        result.append(span(rows, i, end, title, kind + "_block"))
        # Numeric subheadings inside a note reset to 1 and/or use a
        # different separator. They must stay inside the parent note.
        subs, last_number, separator = [], 0, None
        for j in range(i + 1, end):
            title_line = rows[j][2].strip()
            match = re.match(r"^(\d+)([、．.]|\.(?=\s))", title_line)
            if not NUMBERED.match(title_line) or not match:
                continue
            number, mark = int(match[1]), match[2]
            if separator is None:
                separator = mark
            if mark == separator and number > last_number:
                subs.append((j, title_line))
                last_number = number
        for n, (j, sub) in enumerate(subs):
            stop = subs[n + 1][0] if n + 1 < len(subs) else end
            result.append(span(rows, j, stop, sub, kind))
    heads = [
        (i, STATEMENT.match(t.strip()))
        for i, (_, _, t) in enumerate(rows)
        if STATEMENT.match(t.strip())
    ]
    for k, (i, m) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(rows)
        if m[1] != "合并" or "权益变动表" in m[2]:
            continue
        # The next statement or major notes heading bounds the table.
        stops = [
            s["start"]
            for s in result
            if s["kind"].endswith("_block") and s["start"] > i
        ]
        if stops:
            end = min(end, min(stops))
        kind = {"资产负债表": "balance", "利润表": "income", "现金流量表": "cashflow"}[
            m[2]
        ]
        result.append(span(rows, i, end, m[0], kind))
    return result


def row_label(text: str) -> str:
    text = re.sub(r"^\s*\d+[.．、]\s*(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(
        r"(?:附注)?[一二三四五六七八九十]+[、．.]\s*\d+(?:[、．.]?[（(]\d+[）)])?", "", text
    )
    text = re.sub(r"附注[一二三四五六七八九十]+", "", text)
    text = re.sub(r"(?<![\d.,])\d+[、．.][（(]\d+[）)]", "", text)
    text = MONEY.sub("", text)
    text = re.sub(r"\d+(?:[（(]\d+[）)])?", "", text)
    text = re.sub(r"^[一二三四五六七八九十]+[、．.]", "", compact(text))
    text = re.sub(r"^[（(][一二三四五六七八九十]+[）)]", "", text)
    return re.sub(r"^[加减][:：]", "", text)


def cash_payment_sign_basis(section: dict) -> dict:
    """Confirm negative-payment presentation per column, never infer from Capex.

    Require two distinct ordinary payment rows and an outflow subtotal. A lone
    negative purchase/refund in an otherwise positive table remains signed.
    """
    labels = (
        "购买商品、接受劳务支付的现金",
        "支付给职工以及为职工支付的现金",
        "支付给职工及为职工支付的现金",
        "支付的各项税费",
        "投资支付的现金",
    )
    ordinary = [set(), set()]
    totals = [[], []]
    proofs = [[], []]
    positive_rows = [[], []]
    for row in section["lines"]:
        text = row["text"]
        cleaned = row_label(text).rstrip("（）() ")
        is_total = cleaned in {
            "经营活动现金流出小计",
            "投资活动现金流出小计",
            "筹资活动现金流出小计",
        }
        label = next((label for label in labels if cleaned == label), None)
        matches = list(MONEY.finditer(text))
        if len(matches) != 2 or not (is_total or label):
            continue
        for column, match in enumerate(matches):
            value = Decimal(
                match[0]
                .replace(",", "")
                .replace("−", "-")
                .replace("－", "-")
                .replace("—", "-")
            )
            parentheses = text[: match.start()].rstrip().endswith(("(", "（")) and text[
                match.end() :
            ].lstrip().startswith((")", "）"))
            negative = value < 0 or parentheses
            if not negative and value > 0:
                positive_rows[column].append({"page": row["page"], "quote": text})
            if negative:
                if is_total:
                    totals[column].append(text)
                else:
                    ordinary[column].add(label)
                proofs[column].append({"page": row["page"], "quote": text})
    return {
        name: {
            "confirmed": len(ordinary[index]) >= 2
            and bool(totals[index])
            and not positive_rows[index],
            "positive_rows_conflicting_with_negative_convention": positive_rows[index],
            "ordinary_payment_labels": sorted(ordinary[index]),
            "subtotal_quotes": totals[index],
            "evidence": proofs[index],
        }
        for index, name in enumerate(("current", "comparative"))
    }


def extract_field(metric: str, spec: tuple, sections: list, meta: dict) -> dict:
    label, kind, pattern = spec
    result = {
        "metric": metric,
        "label": label,
        "status": "not_located",
        "period_end": meta["period_end"],
        "scope": "consolidated",
        "candidates": [],
    }
    for section in (s for s in sections if s["kind"] == kind):
        payment_basis = cash_payment_sign_basis(section) if kind == "cashflow" else {}
        ls = [
            x
            for x in section["lines"]
            if not re.fullmatch(r"\s*\d+(?:/\d+)?\s*", x["text"])
            and not (
                meta.get("company_name", "\0") in x["text"] and "报告" in x["text"]
            )
        ]
        head = "\n".join(x["text"] for x in ls[:18])
        um = re.search(r"(?:单位\s*[:：]\s*|人民币\s*)(亿元|万元|千元|元)", head)
        scale = (
            {"元": 1, "千元": 1000, "万元": 10000, "亿元": 100000000}.get(um[1])
            if um
            else None
        )
        explicit_currency = (
            "CNY"
            if re.search(r"币种\s*[:：]\s*人民币|人民币\s*(?:亿元|万元|千元|元)", head)
            else None
        )
        year = int(meta["period_end"][:4])
        h = compact(head)
        report_date = meta["period_end"].split("-")
        date_pattern = (
            rf"{report_date[0]}年0?{int(report_date[1])}月0?{int(report_date[2])}日"
        )
        # Only actual table column headings establish year order. The report
        # title's year must never make a reversed table appear current-first.
        column_lines = []
        for header_index, header_line in enumerate(ls[:18]):
            header_text = compact(header_line["text"])
            is_column_label = bool(
                re.match(r"^(?:项目|附注|资产附注|负债和股东权益附注)", header_text)
            )
            standalone_year_columns = (
                header_text.startswith("20")
                and str(year) in header_text
                and str(year - 1) in header_text
            )
            if is_column_label or standalone_year_columns:
                for next_line in ls[header_index : header_index + 5]:
                    if MONEY.search(next_line["text"]):
                        break
                    column_lines.append(next_line["text"])
                if any(
                    str(year) in line or "期末余额" in line for line in column_lines
                ):
                    break
                column_lines = []
        column_header = compact("\n".join(column_lines))
        ordered_columns = bool(
            re.search(str(year) + r".{0,35}" + str(year - 1), column_header)
        )
        comparative_end = str(year - 1) + meta["period_end"][4:]
        if kind == "balance":
            dates = []
            for y, month, day in re.findall(
                r"(20\d{2})年(\d{1,2})月(\d{1,2})日", column_header
            ):
                iso = f"{y}-{int(month):02d}-{int(day):02d}"
                if iso not in dates:
                    dates.append(iso)
            if len(dates) == 2:
                ordered_columns = dates[0] == meta["period_end"] and dates[1] < dates[0]
                comparative_end = dates[1]
            else:
                ordered_columns = (
                    bool(re.search(date_pattern, h))
                    and len(dates) <= 1
                    and (
                        "期末余额期初余额" in column_header
                        or "期末余额年初余额" in column_header
                    )
                )
                comparative_end = f"{year - 1}-12-31"
            if "调整金额" in h:
                ordered_columns = False
        elif meta["period_end"][5:] == "06-30":
            ordered_columns = ordered_columns and bool(
                re.search(r"半年度|1[—－至~～-]6月", h)
            )
        for i in range(len(ls)):
            # A wrapped ownership/continuing-operation label is not a new total
            # net-profit row. Keep its preceding qualifier when deciding scope.
            if metric == "net_profit_total" and i and re.search(
                r"(?:归属于母公司(?:股东|所有者)的|持续经营|终止经营)$",
                compact(ls[i - 1]["text"]),
            ):
                continue
            # A preceding row's amounts cannot become the next wrapped label's values.
            if not row_label(ls[i]["text"]).strip():
                continue
            for width in range(1, 5):
                window = ls[i : i + width]
                # Do not join unrelated physical pages / headers.
                if not window or window[-1]["page"] - window[0]["page"] > 1:
                    break
                text = "\n".join(x["text"] for x in window)
                # Current label may wrap around numeric columns. Earlier full
                # rows cannot be consumed as part of a different metric.
                cleaned = row_label(text)
                matched = re.match(pattern, cleaned)
                if not matched:
                    continue
                # Only the current label and its loss/sign annotation may
                # surround the amounts. Never borrow values from a new row.
                tail = cleaned[matched.end() :]
                if tail and re.search(
                    r'[^（()）损失收益净亏以号填列负数为的：:“”"\-−－—]', tail
                ):
                    continue
                numbers = MONEY.findall(text)
                candidate = {
                    "source_ref": {
                        "document": meta["short_id"],
                        "pages": sorted({x["page"] for x in window}),
                        "line_start": window[0]["line"],
                        "line_end": window[-1]["line"],
                    },
                    "quote": text,
                    "unit": um[1] if um else None,
                    "currency": explicit_currency,
                    "header": head,
                    "header_source_pages": sorted({line["page"] for line in ls[:18]}),
                    "numbers": numbers,
                    "columns_verified": ordered_columns,
                }
                if len(numbers) < 2 and width < 4:
                    result["candidates"].append(candidate)
                    continue
                # Exactly two amount columns are required. Blank cells and
                # ambiguous line wrapping never become zero/current amounts.
                if (
                    len(numbers) == 2
                    and scale
                    and ordered_columns
                    and any(len(MONEY.findall(x["text"])) == 2 for x in window)
                ):
                    vals = []
                    for match in MONEY.finditer(text):
                        value = Decimal(
                            match[0]
                            .replace(",", "")
                            .replace("−", "-")
                            .replace("－", "-")
                            .replace("—", "-")
                        )
                        if text[: match.start()].rstrip().endswith(
                            ("(", "（")
                        ) and text[match.end() :].lstrip().startswith((")", "）")):
                            value = -abs(value)
                        vals.append(value)
                    outflow = metric in CASH_OUTFLOW_FIELDS
                    normalize = [
                        outflow
                        and payment_basis.get(column, {}).get("confirmed", False)
                        for column in ("current", "comparative")
                    ]
                    candidate.update(
                        value=str(vals[0]),
                        comparative_value=str(vals[1]),
                        scale=scale,
                        amount=str((-vals[0] if normalize[0] else vals[0]) * scale),
                        comparative_amount=str(
                            (-vals[1] if normalize[1] else vals[1]) * scale
                        ),
                        reported_amount=str(vals[0] * scale),
                        comparative_reported_amount=str(vals[1] * scale),
                        sign_convention="cash_paid_positive_refund_negative"
                        if outflow and all(normalize)
                        else "mixed_column_cash_payment_convention"
                        if outflow and any(normalize)
                        else "as_reported_signed",
                        cash_payment_sign_basis=payment_basis if outflow else {},
                        comparative_period_end=comparative_end,
                    )
                result["candidates"].append(candidate)
                break
    # Deduplicate equal captures, but never discard conflicting values.
    valid = [c for c in result["candidates"] if "amount" in c]
    amounts = {
        (c["amount"], c["comparative_amount"], c["unit"], c["currency"]) for c in valid
    }
    if len(amounts) > 1:
        result.update(status="conflict", reason="multiple_statement_values")
    elif valid:
        result.update(
            status="obtained", **{k: v for k, v in valid[0].items() if k != "numbers"}
        )
        result["verification"] = (
            "deterministic_row_columns_unit; accounting meaning not certified"
        )
        if not result["currency"]:
            result["currency_status"] = (
                "presentation_currency_not_explicit_in_table_header"
            )
    elif result["candidates"]:
        result.update(
            status="parse_failed",
            reason="ambiguous_columns_or_missing_header; blank is not zero",
        )
    return result


def topic_entry(key: str, spec: tuple, sections: list) -> dict:
    name, labels, facets = spec
    policy = [
        s
        for s in sections
        if s["kind"] == "policy" and any(x in s["title"] for x in labels)
    ]
    notes = [
        s
        for s in sections
        if s["kind"] in {"notes", "other_notes"}
        and any(x in s["title"] for x in labels)
    ]
    if key == "income_tax":
        notes += [
            s
            for s in sections
            if s["kind"] == "other_notes_block" and "税项" in s["title"]
        ]
    mda = [
        s
        for s in sections
        if s["kind"] == "mda" and any(x in s["text"] for x in labels)
    ]
    text = "\n".join(s["text"] for s in policy + notes)
    return {
        "topic": key,
        "label": name,
        "status": "obtained" if policy or notes else "not_located",
        "policy_sections": [s["id"] for s in policy],
        "note_sections": [s["id"] for s in notes],
        "management_sections": [s["id"] for s in mda],
        "policy_origin": "current_report" if policy else "not_located",
        "facets": [
            {
                "request": f,
                "status": "located_pending_review"
                if re.search(f, text)
                else "not_located",
                "section_refs": [
                    s["id"] for s in policy + notes if re.search(f, s["text"])
                ],
            }
            for f in facets
        ],
        "verification": "sections located; keyword presence is not complete accounting coverage",
    }


def apply_policy_reference(pack: dict, prior: dict | None) -> None:
    if not prior or pack["metadata"]["kind"] == "annual":
        return
    now, old = pack["metadata"], prior["metadata"]
    if (
        old["issuer_id"] != now["issuer_id"]
        or old["kind"] != "annual"
        or old["period_end"] >= now["period_end"]
        or old["published_at"] > now["published_at"]
    ):
        return
    for key, topic in pack["accounting_topics"].items():
        if topic["policy_sections"]:
            continue
        ancestor = prior["accounting_topics"][key]
        if ancestor["policy_sections"]:
            topic["policy_reference"] = {
                "pack_key": prior["pack_key"],
                "document": old["short_id"],
                "period_end": old["period_end"],
                "published_at": old["published_at"],
                "section_refs": ancestor["policy_sections"],
                "status": "prior_policy_reference_pending_applicability_review",
            }
            topic["policy_origin"] = "prior_annual_reference_not_current_disclosure"
            topic["current_policy_change_check"] = pack["supplements"][
                "policy_changes"
            ]["status"]


def make_pack(source: dict, pack_key: str, prior: dict | None = None) -> dict:
    meta, pages = source["metadata"], source["pages"]
    sections = locate_sections(pages)
    fields = {
        key: extract_field(key, spec, sections, meta) for key, spec in FIELDS.items()
    }
    supplements = {}
    for key, (label, pattern) in SUPPLEMENTS.items():
        ss = [
            s
            for s in sections
            if s["kind"] in {"chapter", "mda", "notes", "policy", "other_notes"}
            and re.search(pattern, s["text"], re.IGNORECASE)
        ]
        # Keep the smallest available structural sections while retaining broad
        # chapter references for disclosures outside the notes.
        narrow = [s for s in ss if s["kind"] not in {"chapter", "mda"}]
        supplements[key] = {
            "label": label,
            "status": "obtained" if ss else "not_located",
            "section_refs": [s["id"] for s in (narrow or ss)],
            "verification": "source_available; values/definitions not yet structured",
        }
    mda = [s for s in sections if s["kind"] == "mda"]
    pack = {
        "schema_version": "fr-pack/1",
        "parser_version": PARSER_VERSION,
        "pack_key": pack_key,
        "metadata": meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cache_policy": {
            "minimum_retention_days": 365,
            "fixed_source_version_expires": False,
            "invalidation": "source_hash_or_parser_version_or_referenced_policy_version",
        },
        "financial_fields": fields,
        "sections": sections,
        "management_discussion": {
            "status": "obtained" if mda else "not_located",
            "section_refs": [s["id"] for s in mda],
            "attribution": "management_statements_not_verified_investment_claims",
        },
        "accounting_topics": {
            k: topic_entry(k, v, sections) for k, v in TOPICS.items()
        },
        "supplements": supplements,
        "errors": [],
        "external_requests": 0,
        "model_calls": 0,
        "research_acceptance": "not_applicable_data_acquisition_only",
    }
    cfo, capex = fields["cfo"], fields["capex"]
    proxy = {
        "status": "not_located",
        "formula": "cfo - capex",
        "label": "基础现金流代理，非正常化股东FCF",
        "inputs": ["cfo", "capex"],
        "pending_adjustments": [
            "财务公司资金",
            "少数股东",
            "维持/增长Capex",
            "营运资本",
            "利息分类",
            "并购维持性支出",
        ],
    }
    if (
        all(x["status"] == "obtained" for x in (cfo, capex))
        and cfo["unit"] == capex["unit"]
        and cfo["currency"] == capex["currency"]
    ):
        proxy.update(
            status="obtained",
            amount=str(Decimal(cfo["amount"]) - Decimal(capex["amount"])),
            unit="元"
            if cfo["currency"] == "CNY"
            else "原报表货币单位（归一到元，列报币种待核对）",
            currency=cfo["currency"],
            period_end=meta["period_end"],
            scope="consolidated",
        )
    pack["fcf_proxy"] = proxy
    statement_links = {
        "revenue": ["revenue", "contract_assets", "contract_liabilities"],
        "inventory": ["inventory", "cost", "asset_impairment"],
        "ppe": ["ppe", "construction", "capex", "asset_disposal_cash"],
        "intangibles": ["intangibles", "goodwill", "capex", "asset_impairment"],
        "income_tax": ["income_tax", "tax_cash"],
        "investment_income": [
            "investment_income",
            "fair_value_change",
            "credit_impairment",
            "investment_income_cash",
            "investment_cash_in",
            "investment_cash_out",
        ],
        "finance_cost": [
            "finance_cost",
            "interest_expense",
            "interest_income",
            "short_debt",
            "long_debt",
            "bonds",
            "lease_liabilities",
            "distribution_interest_cash",
        ],
    }
    for key, topic in pack["accounting_topics"].items():
        topic["statement_fields"] = statement_links[key]
        topic["policy_change_refs"] = supplements["policy_changes"]["section_refs"]
        topic["full_policy_blocks"] = [
            s["id"] for s in sections if s["kind"] == "policy_block"
        ]
        topic["full_note_blocks"] = [
            s["id"] for s in sections if s["kind"] == "notes_block"
        ]
    apply_policy_reference(pack, prior)
    pack["coverage"] = {
        "financial_fields_obtained": sum(
            f["status"] == "obtained" for f in fields.values()
        ),
        "financial_fields_requested": len(fields),
        "mda_pages": len({p for s in mda for p in s["pages"]}),
        "accounting_topics_located": sum(
            t["status"] == "obtained" for t in pack["accounting_topics"].values()
        ),
        "unresolved_facets": sum(
            f["status"] == "not_located"
            for t in pack["accounting_topics"].values()
            for f in t["facets"]
        ),
        "semantic_coverage_verified": False,
    }
    return pack


def validate_source(source: dict) -> list:
    meta, pages = source.get("metadata", {}), source.get("pages", {})
    errors = []
    required = (
        "document_id",
        "issuer_id",
        "short_id",
        "period_end",
        "published_at",
        "kind",
        "path",
        "sha256",
    )
    if any(not meta.get(k) for k in required):
        errors.append("missing_source_identity_or_version")
    if not pages or set(map(int, pages)) != set(
        range(1, int(meta.get("page_count", len(pages))) + 1)
    ):
        errors.append("missing_or_noncontinuous_pages")
    if any(not str(t).strip() for t in pages.values()):
        errors.append("empty_page_text_requires_visual_retrieval")
    pdf = Path(meta.get("path", ""))
    text_path = pdf.with_name("document.txt")
    if text_path.is_file():
        text = text_path.read_text(encoding="utf-8")
        if digest(text.encode()) != meta.get("text_sha256"):
            errors.append("source_text_hash_mismatch")
        chunks = re.split(r"--- PAGE (\d+) ---", text)
        original = {chunks[i]: chunks[i + 1] for i in range(1, len(chunks), 2)}
        if original != pages:
            errors.append("source_pages_differ_from_frozen_text")
    else:
        errors.append("frozen_source_text_missing")
    if not pdf.is_file() or digest(pdf.read_bytes()) != meta.get("sha256"):
        errors.append("pdf_missing_or_hash_mismatch")
    else:
        import pypdfium2

        try:
            with pypdfium2.PdfDocument(str(pdf)) as original_pdf:
                if len(original_pdf) != len(pages):
                    errors.append("pdf_physical_page_count_mismatch")
        except pypdfium2.PdfiumError:
            errors.append("original_pdf_unreadable")
    if meta.get("company_name") and meta["company_name"] not in str(pages.get("1", "")):
        errors.append("cover_company_not_matched")
    return errors


def build_all(source_dir: Path, output: Path, document_ids: list | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    sources, load_errors = [], []
    for source_path in sorted((source_dir / "sources").glob("*.json")):
        try:
            source = read_json(source_path)
            if not isinstance(source, dict):
                raise TypeError("invalid_source_contract")
            if not isinstance(source.get("metadata"), dict) or not isinstance(
                source.get("pages"), dict
            ):
                raise TypeError("invalid_source_contract")
            if any(
                k not in source["metadata"]
                for k in ("issuer_id", "period_end", "published_at", "short_id", "kind")
            ):
                raise ValueError("missing_metadata")
            sources.append(source)
        except (ValueError, KeyError, TypeError, OSError) as error:
            load_errors.append(
                {
                    "document": source_path.stem,
                    "status": "source_blocked",
                    "errors": [str(error)],
                }
            )
    sources.sort(
        key=lambda s: (
            s["metadata"]["issuer_id"],
            s["metadata"]["period_end"],
            s["metadata"]["published_at"],
        )
    )
    selected = set(document_ids or [s["metadata"]["short_id"] for s in sources])
    dependencies = set()
    for source in sources:
        m = source["metadata"]
        if m["short_id"] not in selected or m["kind"] == "annual":
            continue
        previous = [
            s["metadata"]
            for s in sources
            if s["metadata"]["issuer_id"] == m["issuer_id"]
            and s["metadata"]["kind"] == "annual"
            and s["metadata"]["period_end"] < m["period_end"]
            and s["metadata"]["published_at"] <= m["published_at"]
        ]
        if previous:
            prior_meta = max(
                previous, key=lambda v: (v["period_end"], v["published_at"])
            )
            dependencies.add(prior_meta["short_id"])
    entries, built = load_errors, []
    for source in sources:
        meta = source["metadata"]
        if meta["short_id"] not in selected | dependencies:
            continue
        try:
            errors = validate_source(source)
            if errors:
                entries.append(
                    {
                        "document": meta["short_id"],
                        "status": "source_blocked",
                        "errors": errors,
                    }
                )
                continue
            candidates = [
                p
                for p in built
                if p["metadata"]["issuer_id"] == meta["issuer_id"]
                and p["metadata"]["kind"] == "annual"
                and p["metadata"]["period_end"] < meta["period_end"]
                and p["metadata"]["published_at"] <= meta["published_at"]
            ]
            prior = candidates[-1] if candidates and meta["kind"] != "annual" else None
            source_hash = digest(
                json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
            )
            key = digest(
                json.dumps(
                    [source_hash, PARSER_VERSION, prior["pack_key"] if prior else None]
                ).encode()
            )
            path = output / "packs" / key / "pack.json"
            receipt = path.with_name("receipt.json")
            cache_hit = False
            if path.exists() and receipt.exists():
                if (
                    read_json(receipt).get("pack_sha256") == digest(path.read_bytes())
                    and digest(
                        json.dumps(
                            read_json(path.with_name("source.json")),
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode()
                    )
                    == source_hash
                ):
                    pack = read_json(path)
                    cache_hit = pack.get("pack_key") == key
                else:
                    raise ValueError("immutable_cached_pack_corrupt")
            if not cache_hit:
                pack = make_pack(source, key, prior)
                pack["source_json_sha256"] = source_hash
                write_json(path, pack)
                write_json(
                    receipt,
                    {
                        "pack_sha256": digest(path.read_bytes()),
                        "source_json_sha256": source_hash,
                    },
                )
                write_json(path.with_name("source.json"), source)
            built.append(pack)
            entries.append(
                {
                    "document": meta["short_id"],
                    "company": meta["company_name"],
                    "period_end": meta["period_end"],
                    "status": "materialized_with_visible_gaps",
                    "pack_key": key,
                    "cache_hit": cache_hit,
                    "dependency_only": meta["short_id"] not in selected,
                    "coverage": pack["coverage"],
                }
            )

        except (ValueError, KeyError, TypeError, OSError) as error:
            entries.append(
                {
                    "document": meta.get("short_id"),
                    "status": "failed",
                    "errors": [f"{type(error).__name__}: {error}"],
                }
            )
    found = {e["document"] for e in entries}
    for missing in sorted(set(document_ids or []) - found):
        entries.append(
            {
                "document": missing,
                "status": "failed",
                "errors": ["requested_document_not_found"],
            }
        )
    if not sources:
        entries.append(
            {
                "document": None,
                "status": "failed",
                "errors": ["no_frozen_sources_found"],
            }
        )
    result = {
        "schema_version": "fr-pack-run/1",
        "entries": entries,
        "documents_requested": len(document_ids) if document_ids else len(sources),
        "external_requests": 0,
        "model_calls": 0,
        "built": sum(not e["cache_hit"] for e in entries if "cache_hit" in e),
        "cache_hits": sum(e.get("cache_hit", False) for e in entries),
        "acceptance": "data_materialization_only_not_semantic_or_research_acceptance",
    }
    write_json(output / "latest_run.json", result)
    return result


def verified_pack(folder: Path) -> tuple:
    """Fail visibly before render/audit if either immutable artifact changed."""
    pack_path = folder / "pack.json"
    receipt = read_json(folder / "receipt.json")
    if digest(pack_path.read_bytes()) != receipt["pack_sha256"]:
        raise ValueError("immutable_cached_pack_corrupt")
    pack, source = read_json(pack_path), read_json(folder / "source.json")
    source_hash = digest(
        json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
    )
    if (
        source_hash != receipt["source_json_sha256"]
        or source_hash != pack["source_json_sha256"]
    ):
        raise ValueError("immutable_cached_source_corrupt")
    if pack["metadata"] != source["metadata"]:
        raise ValueError("pack_source_identity_mismatch")
    return pack, source


def audit_all(output: Path) -> dict:
    """Offline numerical/source replay; no claim of semantic acceptance."""
    run = read_json(output / "latest_run.json")
    checks = []
    for entry in run["entries"]:
        errors = list(entry.get("errors", []))
        if "pack_key" in entry:
            try:
                pack, source = verified_pack(output / "packs" / entry["pack_key"])
                errors.extend(validate_source(source))
                replay = make_pack(source, pack["pack_key"])
                for key in (
                    "financial_fields",
                    "sections",
                    "fcf_proxy",
                    "management_discussion",
                    "supplements",
                ):
                    if replay[key] != pack[key]:
                        errors.append(f"replay_mismatch:{key}")
                for name, topic in pack["accounting_topics"].items():
                    base = replay["accounting_topics"][name]
                    for key in ("policy_sections", "note_sections", "facets"):
                        if base[key] != topic[key]:
                            errors.append(f"topic_replay_mismatch:{name}:{key}")
                    if topic.get("policy_reference"):
                        ref = topic["policy_reference"]
                        annual, _ = verified_pack(output / "packs" / ref["pack_key"])
                        probe = make_pack(source, pack["pack_key"], annual)
                        if (
                            probe["accounting_topics"][name].get("policy_reference")
                            != ref
                        ):
                            errors.append(f"invalid_policy_reference:{name}")
            except (ValueError, KeyError, TypeError, OSError) as error:
                errors.append(str(error))
        checks.append(
            {"document": entry["document"], "passed": not errors, "errors": errors}
        )
    result = {
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "external_requests": 0,
        "model_calls": 0,
        "meaning": "source_integrity_and_deterministic_replay_only; not accounting interpretation acceptance",
    }
    write_json(output / "offline_checks.json", result)
    return result
