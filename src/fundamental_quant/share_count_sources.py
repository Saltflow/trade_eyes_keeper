"""Closing ordinary-share evidence from original share-change disclosures.

No RMB share-capital amount is converted to shares. Unchanged-count inheritance
requires an explicit current-period statement and a verified previous year end.
Counts include issued A/H shares and uncancelled treasury stock; they are not
weighted-average EPS shares or dividend-entitled shares.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

VERSION = "closing-share-source/1"
LABEL = re.compile(r"(?:普通股)?股\s*份\s*总\s*数")
NUMBER = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?%?")
UNCHANGED = re.compile(r"报告期内[，,]?公司股份总数及股本结构未发生变化")


def _compact(text):
    return "".join(str(text).split())


def _number(token):
    return Decimal(token.replace(",", "").replace("%", ""))


def _ref(doc, page):
    return f"{doc['document_id']}:p{page}"


def _candidate(doc, value, period, refs, quote, method, **extra):
    return {
        "issuer_id": doc["issuer_id"],
        "company_name": doc.get("company_name", ""),
        "metric": "shares_outstanding",
        "period": period,
        "original_column_period": period,
        "column": "current" if period == doc["period_end"] else "comparative",
        "kind": "FY"
        if period.endswith("12-31")
        else "H1"
        if period.endswith("06-30")
        else "YTD",
        "value": int(value),
        "raw_amount": str(int(value)),
        "unit": "股",
        "currency": None,
        "scope": "consolidated",
        "published_at": doc["published_at"],
        "document_id": doc["document_id"],
        "quote": quote,
        "header": "ordinary issued share count; not RMB share capital",
        "refs": refs,
        "method": method,
        "method_version": VERSION,
        "denominator_basis": "issued_ordinary_shares_including_uncancelled_treasury",
        **extra,
    }


def _table_candidates(doc):
    pages = doc["pages"]
    opening_period = f"{int(doc['period_end'][:4]) - 1}-12-31"
    for page_no in sorted(pages, key=int):
        text = pages[page_no]
        prior_no = str(int(page_no) - 1)
        prior = pages.get(prior_no, "")
        context = _compact(prior + text)
        if not all(
            term in context for term in ("本次变动前", "本次变动后", "数量", "比例")
        ):
            continue
        if not any(term in context for term in ("股份变动情况", "股本变动情况")):
            continue
        for match in LABEL.finditer(text):
            before = _compact(prior + text[: match.start()])
            # Event-specific before/after tables do not establish FY opening
            # counts. Require the standard full-period movement columns.
            local_header = before.rsplit("本次变动前", 1)[-1]
            if not all(
                term in local_header
                for term in ("本次变动增减", "发行新股", "送股", "公积金转股")
            ):
                continue
            units = list(re.finditer(r"单位[:：](万股|股|万元|元)", before))
            if not units or units[-1].group(1) != "股":
                continue
            numeric = re.match(r"[\s0-9,.%+－–−-]+", text[match.end() :])
            if not numeric:
                continue
            raw = numeric.group(0)
            # Thousand separators may wrap to the next line, never merge cells.
            normalized = (
                re.sub(r",\s+(?=\d)", ",", raw)
                .replace("－", "-")
                .replace("–", "-")
                .replace("−", "-")
            )
            normalized = re.sub(
                r"(\d{1,3}(?:,\d{3})*,)(\d{1,2})\s+(\d{1,2})(?=\s|$)",
                lambda m: m[1] + m[2] + m[3] if len(m[2] + m[3]) == 3 else m[0],
                normalized,
            )
            tokens = NUMBER.findall(normalized)
            if len(tokens) < 4:
                continue
            values = [_number(t) for t in tokens]
            totals = [i for i, v in enumerate(values) if v == 100]
            if len(totals) < 2 or totals[0] != 1:
                continue
            close_index = totals[-1] - 1
            opening, closing = values[0], values[close_index]
            if (
                opening <= 1000
                or closing <= 1000
                or opening != int(opening)
                or closing != int(closing)
            ):
                continue
            # Change table's final movement column is the subtotal. This guards
            # against interpreting an adjacent class, percentage or broken cell.
            if close_index > 2:
                if values[close_index - 1] != closing - opening:
                    continue
            elif opening != closing:
                continue
            refs = [_ref(doc, page_no)]
            same_page_header = _compact(text[: match.start()])
            if (
                not re.search(r"单位[:：]股", same_page_header)
                or "本次变动前" not in same_page_header
            ):
                refs.insert(0, _ref(doc, prior_no))
            quote = text[match.start() : match.end() + numeric.end()]
            yield _candidate(
                doc,
                opening,
                opening_period,
                refs,
                quote,
                "share_movement_table_opening",
            )
            yield _candidate(
                doc,
                closing,
                doc["period_end"],
                refs,
                quote,
                "share_movement_table_closing",
            )


def _dated_candidates(doc):
    end = date.fromisoformat(doc["period_end"])
    date_pattern = rf"{end.year}年0?{end.month}月0?{end.day}日"
    pattern = re.compile(
        rf"截至{date_pattern}[，,]?(?:本)?公司(?:普通股)?总股本(?:为|共|合计)?([0-9,]+)(万股|股)"
    )
    for page_no, text in doc["pages"].items():
        compact = _compact(text)
        for match in pattern.finditer(compact):
            value = _number(match.group(1)) * (10000 if match.group(2) == "万股" else 1)
            if value <= 0 or value != int(value):
                continue
            yield _candidate(
                doc,
                value,
                doc["period_end"],
                [_ref(doc, page_no)],
                match.group(0),
                "explicit_period_end_share_statement",
                quote_normalization="whitespace_only",
            )


def extract_share_count_observations(documents):
    """Return observations and visible gaps/conflicts, without mutating inputs.

    Input is the frozen ComparisonPanel documents list, including original pages.
    Inheritance never jumps a missing full year and never assumes unchanged stock.
    """
    observations, issues, verified, conflicted = [], [], {}, set()
    ordered = sorted(
        documents, key=lambda d: (d["issuer_id"], d["period_end"], d["published_at"])
    )
    for doc in ordered:
        candidates = list(_table_candidates(doc)) + list(_dated_candidates(doc))
        key = (doc["issuer_id"], doc["period_end"])
        current = [c for c in candidates if c["period"] == doc["period_end"]]
        if not current:
            previous_key = (doc["issuer_id"], f"{int(doc['period_end'][:4]) - 1}-12-31")
            previous = verified.get(previous_key)
            unchanged = [
                (p, UNCHANGED.search(_compact(t))) for p, t in doc["pages"].items()
            ]
            unchanged = [(p, m) for p, m in unchanged if m]
            if (
                previous
                and unchanged
                and previous_key not in conflicted
                and previous["published_at"][:10] <= doc["published_at"][:10]
            ):
                p, match = unchanged[0]
                current = [
                    _candidate(
                        doc,
                        previous["value"],
                        doc["period_end"],
                        list(dict.fromkeys(previous["refs"] + [_ref(doc, p)])),
                        match.group(0),
                        "explicit_unchanged_from_verified_prior_year_end",
                        inherited_from={
                            "period": previous["period"],
                            "document_id": previous["document_id"],
                            "quote": previous["quote"],
                            "refs": previous["refs"],
                        },
                    )
                ]
                candidates.extend(current)
        if not current:
            issues.append(
                {
                    "code": "closing_share_count_not_located",
                    "issuer_id": key[0],
                    "period": key[1],
                    "document_id": doc["document_id"],
                }
            )
            continue
        if len({c["value"] for c in current}) != 1:
            verified.pop(key, None)
            conflicted.add(key)
            issues.append(
                {
                    "code": "closing_share_count_conflict",
                    "issuer_id": key[0],
                    "period": key[1],
                    "candidates": current,
                }
            )
            continue
        prior = verified.get(key)
        if prior and prior["value"] != current[0]["value"]:
            # Preserve both observed revisions for the panel conflict policy;
            # neither becomes a silent inheritance base for a later period.
            verified.pop(key, None)
            conflicted.add(key)
            issues.append(
                {
                    "code": "closing_share_count_revision_conflict",
                    "issuer_id": key[0],
                    "period": key[1],
                }
            )
        elif key not in conflicted:
            verified[key] = current[0]
        observations.extend(candidates)
    return {"version": VERSION, "observations": observations, "issues": issues}
