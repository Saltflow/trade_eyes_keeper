"""Evidence-bearing extraction targets and bounded original currency context.

These helpers select context; they never mint facts or certify accounting
interpretation. The extraction contract must still validate every proposal.
"""

from __future__ import annotations

import re

RETRIEVAL_VERSION = "requested-row-coverage/3"

DA_ROW_LABELS = (
    "固定资产折旧",
    "使用权资产折旧",
    "无形资产摊销",
    "长期待摊费用摊销",
    "折旧及摊销",
    "折旧和摊销",
    "折旧与摊销",
    "depreciationandamortization",
)


def _numeric_row_window(text: str) -> bool:
    """Require amount-like cells, not section numbers or page footers."""
    return bool(
        re.search(r"(?<![\w])[-−－]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)", text)
        or re.search(r"(?m)^.*\s[-−－]?\d+\s+[-−－]?\d+\s*$", text)
    )


BALANCE_METRICS = {
    "parent_equity",
    "total_assets",
    "total_liabilities",
    "minority_equity",
    "cash_and_cash_equivalents",
    "short_term_borrowings",
    "long_term_borrowings",
    "current_portion_noncurrent_debt",
    "bonds_payable",
    "lease_liabilities",
    "restricted_cash",
}

from .currency_definitions import (
    find_currency_definitions,
    find_statement_currency_declarations,
    same_filing,
    statement_context_pages,
)


def matching_metric_rows(source: dict, metrics, hints: dict) -> set[str]:
    """Match requested labels near printed numbers, including wrapped PDF rows.

    A prose mention elsewhere on a page is insufficient. A tight three-line
    window supports PDF layouts putting numeric cells before the row label.
    This is deliberately a retrieval predicate, not a row/value association.
    """
    lines = source.get("text", "").lower().splitlines()
    matches = set()
    for index in range(len(lines)):
        label_window = re.sub(
            r"[\d,.%％]+", "", "".join("".join(lines[index : index + 3]).split())
        )
        numeric_window = "\n".join(lines[max(0, index - 1) : index + 4])
        if not _numeric_row_window(numeric_window):
            continue
        for metric in metrics:
            if metric == "depreciation_amortization" and (
                not any(label in label_window for label in DA_ROW_LABELS)
                or any(term in label_window for term in ("方法", "折旧率", "摊销率"))
            ):
                continue
            if metric in {"interest_paid", "interest_not_in_cfo"} and any(
                term in label_window for term in ("未支付的利息", "unpaidinterest")
            ):
                continue
            if any(
                "".join(term.lower().split()) in label_window
                for term in hints.get(metric, ())
            ):
                matches.add(metric)
    return matches


def requested_row_cells(source: dict, requests: list[dict], hints: dict) -> set[tuple]:
    """Keep metric and fiscal period paired, including annual comparatives."""
    end = str(source.get("period_end", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        return set()
    year = int(end[:4])
    interim = (source.get("document_kind") or source.get("kind")) == "interim"
    matches = matching_metric_rows(source, {r["metric"] for r in requests}, hints)
    return {
        (r["metric"], r["period_end"])
        for r in requests
        if r["metric"] in matches
        and (
            (
                r["period_end"][5:] == end[5:]
                and year - (1 if interim else 2) <= int(r["period_end"][:4]) <= year
            )
            or (
                interim
                and r["metric"] in BALANCE_METRICS
                and r["period_end"] == f"{year - 1}-12-31"
            )
        )
    }


def statement_rank(source: dict, sources: list[dict], metrics, hints: dict) -> int:
    """Prefer consolidated table rows; continuation inherits nearest heading.

    A parent heading AFTER the requested row must not hide preceding consolidated
    rows. This ranks retrieval only; fact-level scope validation remains required.
    """
    text = re.sub(r"\s+", "", source.get("text", "")).lower()
    positions = [
        text.find(re.sub(r"\s+", "", term.lower()))
        for metric in metrics
        for term in hints.get(metric, ())
    ]
    positions = [p for p in positions if p >= 0]
    before = text[: min(positions)] if positions else text
    heading = re.compile(r"(合并|母公司)(?:资产负债表|利润表|现金流量表)")
    found = list(heading.finditer(before))
    if not found:
        related = sorted(
            (
                s
                for s in sources
                if same_filing(source, s)
                and 0 < int(source["page"]) - int(s["page"]) <= 4
            ),
            key=lambda s: int(s["page"]),
            reverse=True,
        )
        for item in related:
            found = list(heading.finditer(re.sub(r"\s+", "", item.get("text", ""))))
            if found:
                break
    if found:
        return 0 if found[-1].group(1) == "合并" else 2
    return 1


def select_requested_pages(pages, requests, hints, context, limit):
    """Cover distinct requested cells before redundant pages from a filing.

    Screen-keyword density must not crowd one missing cashflow row out of every
    batch. Prefer consolidated tables, then current-period evidence, while
    retaining summary/notes evidence if no statement row is available.
    """
    remaining = list(pages)
    cells = {s["id"]: requested_row_cells(s, requests, hints) for s in pages}
    uncovered = set().union(*cells.values()) if cells else set()
    selected = []
    while remaining and uncovered and len(selected) < limit:

        def score(source, uncovered=uncovered):
            novel = cells[source["id"]] & uncovered
            relevant = novel or cells[source["id"]]
            return (
                not bool(novel),
                # A cashflow reconciliation shows the D&A components together;
                # a single fixed-asset note is not a total D&A observation.
                0
                if "depreciation_amortization" in {m for m, _ in relevant}
                and "现金流量表补充资料" in re.sub(r"\s+", "", source.get("text", ""))
                else 1,
                statement_rank(source, context, {m for m, _ in relevant}, hints),
                -len(novel),
                -sum(p == source.get("period_end") for _, p in relevant),
                int(source["page"]),
                source["id"],
            )

        chosen = min(remaining, key=score)
        selected.append(chosen)
        uncovered -= cells[chosen["id"]]
        remaining.remove(chosen)
    return selected


def currency_context_sources(source: dict, sources: list[dict]) -> list[dict]:
    """Return same-filing declarations plus contiguous statement context.

    Includes intervening pages needed for fact-level scope verification. A
    declaration offered to the model is not permission to apply it report-wide.
    """
    related = [item for item in sources if same_filing(source, item)]
    declarations = [
        item
        for item in related
        if find_currency_definitions(item.get("text", ""))
        or find_statement_currency_declarations(item.get("text", ""))
    ]
    context = statement_context_pages(source, related)
    # Reconciliation tables often split D&A components at the page boundary.
    # Supply the next original page as context; never synthesize a total here.
    compact = re.sub(r"\s+", "", source.get("text", ""))
    if "现金流量表补充资料" in compact and any(s in compact for s in DA_ROW_LABELS):
        context = [
            *context,
            *(s for s in related if int(s.get("page", 0)) == int(source["page"]) + 1),
        ]
    return list({item["id"]: item for item in [*declarations, *context]}.values())
