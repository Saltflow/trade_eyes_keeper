"""Narrow consolidated debt-note totals with statement comparative reconciliation."""

from __future__ import annotations

import re
from decimal import Decimal

NOTE_FIELDS = {"应付债券": "bonds"}
NUMBER = r"(?:-?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|-?\d+(?:\.\d{2})?)"


def apply_note_totals(fields, sections, meta):
    """A printed note total may supply a blank statement cell, never infer zero.

    Require the first two-column closing/opening table of an exact consolidated
    debt note and reconcile its opening total to coordinate-verified statements.
    Movement, gross-balance, parent-company or multiple totals stay unresolved.
    """
    for section in sections:
        title = re.sub(r"^\s*\d+\s*[、．.]\s*", "", section["title"]).strip()
        metric = NOTE_FIELDS.get(title)
        if section["kind"] != "notes" or not metric:
            continue
        field = fields[metric]
        if field["status"] not in {"not_located", "parse_failed"}:
            continue
        statements = [
            c
            for c in field.get("candidates", [])
            if c.get("coordinate_evidence", {}).get("column") == "comparative"
            and c.get("comparative_period_end")
            == str(int(meta["period_end"][:4]) - 1) + "-12-31"
        ]
        if not statements:
            continue
        rows = section["lines"]
        header = next(
            (
                i
                for i, r in enumerate(rows)
                if re.sub(r"\s", "", r["text"]) == "项目期末余额期初余额"
            ),
            None,
        )
        if header is None or header > 10:
            continue
        preamble = "\n".join(r["text"] for r in rows[:header])
        unit = re.search(r"单位\s*[:：]\s*(亿元|万元|千元|元)", preamble)
        if not unit or not re.search(r"币种\s*[:：]\s*人民币", preamble):
            continue
        tail = []
        for row in rows[header + 1 :]:
            if re.match(r"\s*[（(]\d+[).）]", row["text"]):
                break
            tail.append(row)
        totals = [
            (
                r,
                re.fullmatch(
                    r"\s*合计\s+(" + NUMBER + r")\s+(" + NUMBER + r")\s*", r["text"]
                ),
            )
            for r in tail
        ]
        totals = [(r, m) for r, m in totals if m]
        if len(totals) != 1:
            continue
        row, match = totals[0]
        scale = {"元": 1, "千元": 1000, "万元": 10000, "亿元": 100000000}[unit[1]]
        current, prior = (Decimal(match[i].replace(",", "")) * scale for i in (1, 2))
        if any(Decimal(c["comparative_amount"]) != prior for c in statements):
            continue
        candidate = {
            "amount": str(current),
            "value": match[1].replace(",", ""),
            "comparative_amount": str(prior),
            "comparative_value": match[2].replace(",", ""),
            "scale": scale,
            "unit": unit[1],
            "currency": "CNY",
            "comparative_period_end": statements[0]["comparative_period_end"],
            "quote": row["text"],
            "header": preamble + "\n" + rows[header]["text"],
            "source_ref": {
                "document": meta["short_id"],
                "pages": [row["page"]],
                "line_start": row["line"],
                "line_end": row["line"],
            },
            "note_total_evidence": {
                "method": "consolidated_debt_note_total/1",
                "section_id": section["id"],
                "title": title,
                "statement_comparatives": statements,
                "pdf_sha256": meta["sha256"],
                "meaning": "explicit printed closing total; not an inferred zero",
            },
        }
        field["statement_cell_status"] = field.get("cell_status")
        field["cell_status"] = {
            "current": "obtained_from_note",
            "comparative": "obtained",
        }
        field.update(
            status="obtained",
            verification="note_total_and_statement_comparative_reconciled",
            **candidate,
        )
        field.pop("reason", None)
        field.setdefault("candidates", []).append(candidate)
