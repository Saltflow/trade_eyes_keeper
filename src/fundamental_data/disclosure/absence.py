"""Explicit source absences for optional fields, never inferred from blank cells."""

from __future__ import annotations

import re

VERSION = "fr-explicit-absence/1"
# Amount-note titles only. Policies, subordinate disclosures and combined titles
# cannot exclude an entire financial metric. Critical fields are never listed.
AMOUNT_NOTE_METRICS = {
    "合同资产": "contract_assets",
    "商誉": "goodwill",
    "短期借款": "short_debt",
    "长期借款": "long_debt",
    "应付债券": "bonds",
    "其他权益工具": "other_equity",
    "库存股": "treasury_stock",
    "公允价值变动收益": "fair_value_change",
    "资产减值损失": "asset_impairment",
    "取得子公司及其他营业单位支付的现金净额": "acquisition_cash",
}


def apply_explicit_absences(fields, sections, meta):
    """Mutate missing optional fields only when the entire amount note says none.

    Raw statement candidates remain retained. A numeric candidate, even currently
    ambiguous, vetoes automatic exclusion and must be reconciled by a researcher.
    """
    reviewed = []
    for section in sections:
        if section["kind"] != "notes":
            continue
        title = re.sub(r"^\s*\d+\s*[、．.]\s*", "", section["title"]).strip()
        metric = AMOUNT_NOTE_METRICS.get(title)
        if metric is None or metric in reviewed:
            continue
        field = fields[metric]
        if field["status"] not in {"not_located", "parse_failed"}:
            continue
        if any(c.get("numbers") for c in field.get("candidates", [])):
            continue
        lines = [r for r in section["lines"] if r["text"].strip()]
        if len(lines) != 2 or lines[0]["text"].strip() != section["title"].strip():
            continue
        body = re.sub(r"\s+", "", lines[1]["text"])
        if body not in {
            "无",
            "无。",
            "不适用",
            "不适用。",
            "□适用√不适用",
            "□适用☑不适用",
            "□适用不适用",
        }:
            continue
        # Never carry an absence across a page boundary or from a generic policy.
        if lines[0]["page"] != lines[1]["page"]:
            continue
        field.update(
            status="not_applicable",
            reason="explicit_consolidated_amount_note_absence",
            quote="\n".join(r["text"] for r in lines),
            source_ref={
                "document": meta["short_id"],
                "pages": [lines[0]["page"]],
                "line_start": lines[0]["line"],
                "line_end": lines[-1]["line"],
            },
            exclusion_evidence={
                "version": VERSION,
                "topic": title,
                "section_id": section["id"],
                "scope": "consolidated_amount_note",
                "period_end": meta["period_end"],
                "meaning": "explicit absence; no numeric zero inferred",
            },
        )
        reviewed.append(metric)
    return reviewed
