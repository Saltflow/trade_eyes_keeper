"""Audit source-limited blanks separately from failed financial extraction."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .contract import FIELDS
from .layout import blank_current_cell

VERSION = "fr-disclosure-review/2"
RECEIPT_VERSION = "fr-disclosure-receipt/1"
RECEIPT_METHOD = "original-pdf-review/1"
RECEIPT_DIR = (
    Path(__file__).resolve().parents[3] / "data/fundamental_quant/disclosure_reviews"
)
REVIEW_CLASSES = {
    "notes_comparative_only",
    "net_balance_not_separately_printed",
    "prior_standard_label_absent",
    "statement_blank_verified",
    "gross_amount_not_net_balance",
}


def disclosure_review_hash(source):
    path = RECEIPT_DIR / (str(source["metadata"].get("sha256", "")) + ".json")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def apply_review_receipt(health, fields, source):
    """Classify verified gaps without replacing subsequently obtained amounts."""
    meta = source["metadata"]
    path = RECEIPT_DIR / (str(meta.get("sha256", "")) + ".json")
    if not path.is_file():
        return
    try:
        receipt = json.loads(path.read_text(encoding="utf8"))
        if (
            receipt.get("schema_version") != RECEIPT_VERSION
            or receipt.get("method_version") != RECEIPT_METHOD
        ):
            raise ValueError("receipt_method_version_mismatch")
        if any(
            receipt.get(k) != meta.get(k) for k in ("issuer_id", "period_end")
        ) or receipt.get("pdf_sha256") != meta.get("sha256"):
            raise ValueError("receipt_source_identity_mismatch")
        if (
            hashlib.sha256(Path(meta["path"]).read_bytes()).hexdigest()
            != meta["sha256"]
        ):
            raise ValueError("receipt_original_pdf_hash_mismatch")
        if not receipt.get("reviewer") or not isinstance(receipt.get("fields"), dict):
            raise ValueError("receipt_review_incomplete")
        clean = lambda value: re.sub(r"\s+", "", str(value))
        for metric, reviewed in receipt["fields"].items():
            if fields.get(metric, {}).get("status") == "obtained":
                continue
            if (
                health["details"].get(metric, {}).get("state") == "conflict"
                or metric not in health["details"]
                or reviewed.get("classification") not in REVIEW_CLASSES
                or not reviewed.get("reason")
            ):
                raise ValueError(f"receipt_field_invalid:{metric}")
            evidence = reviewed.get("evidence", [])
            if not evidence or any(
                not e.get("quote")
                or clean(e["quote"])
                not in clean(source["pages"].get(str(e.get("page")), ""))
                for e in evidence
            ):
                raise ValueError(f"receipt_literal_not_found:{metric}")
        # Validate the whole receipt before applying any part.
        for metric, reviewed in receipt["fields"].items():
            if fields[metric].get("status") == "obtained":
                continue
            review = health["details"][metric]
            review["automated_state"] = review["state"]
            review.update(
                state="manual_verified_disclosure_gap",
                manual_review=reviewed,
                reviewer=receipt["reviewer"],
                receipt_sha256=disclosure_review_hash(source),
            )
            field = fields[metric]
            field.setdefault("raw_extraction_status", field["status"])
            field.setdefault("raw_extraction_reason", field.get("reason"))
            field.update(
                status="not_disclosed",
                reason="manual_verified_source_disclosure_gap; numeric value remains unavailable",
            )
        health["receipt_sha256"] = disclosure_review_hash(source)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        health["receipt_errors"] = [f"{type(exc).__name__}:{exc}"]


def verified_optional_disclosure(field, metadata, source, *, source_verified=False):
    """Return replayable proof for a source gap, never a numeric value.

    Caller verifies the actual PDF digest once per assessment. Stored state alone
    is insufficient: original pages and the exact reviewed receipt must match.
    Critical-field and confirmed-error checks remain the caller's responsibility.
    """
    review = field.get("disclosure_review", {})
    if (
        not source_verified
        or field.get("status") != "not_disclosed"
        or field.get("raw_extraction_status") == "conflict"
        or review.get("version") not in {VERSION, "fr-disclosure-review/1"}
        or review.get("pdf_sha256") != metadata.get("sha256")
        or review.get("period_end") != metadata.get("period_end")
        or field.get("period_end") != metadata.get("period_end")
        or field.get("scope", "consolidated") != "consolidated"
    ):
        return None
    clean = lambda value: re.sub(r"\s+", "", str(value))
    pages = source.get("pages", {})

    def literal(quote, page_numbers):
        return (
            bool(quote)
            and bool(page_numbers)
            and all(str(page) in pages for page in page_numbers)
            and clean(quote)
            in clean("\n".join(pages[str(page)] for page in page_numbers))
        )

    state = review.get("state")
    if state == "verified_blank":
        statement = review.get("statement_evidence") or {}
        ref = statement.get("source_ref", {})
        coordinates = statement.get("coordinate_evidence") or {}
        blocks = review.get("searched_note_blocks", [])
        if (
            not literal(statement.get("quote"), ref.get("pages"))
            or ref.get("pdf_sha256", metadata["sha256"]) != metadata["sha256"]
            or coordinates.get("method") != "pdf_numeric_glyph_and_image_cell_check/1"
            or coordinates.get("pdf_sha256") != metadata["sha256"]
            or coordinates.get("page") not in ref.get("pages", [])
            or len(coordinates.get("current_cell_bbox", [])) != 4
            or not coordinates.get("row_boxes")
            or not blocks
            or any(
                not block.get("section_id")
                or not block.get("pages")
                or any(str(page) not in pages for page in block["pages"])
                for block in blocks
            )
            or review.get("note_numeric_candidates")
        ):
            return None
    elif state == "manual_verified_disclosure_gap":
        path = RECEIPT_DIR / (metadata["sha256"] + ".json")
        try:
            raw = path.read_bytes()
            receipt = json.loads(raw)
            item = receipt.get("fields", {}).get(field["metric"], {})
            if (
                hashlib.sha256(raw).hexdigest() != review.get("receipt_sha256")
                or receipt.get("schema_version") != RECEIPT_VERSION
                or receipt.get("method_version") != RECEIPT_METHOD
                or receipt.get("pdf_sha256") != metadata["sha256"]
                or any(
                    receipt.get(k) != metadata.get(k)
                    for k in ("issuer_id", "period_end")
                )
                or not receipt.get("reviewer")
                or item != review.get("manual_review")
                or item.get("classification") not in REVIEW_CLASSES
                or not item.get("reason")
                or not item.get("evidence")
                or any(
                    not literal(e.get("quote"), [e.get("page")])
                    for e in item["evidence"]
                )
            ):
                return None
        except (OSError, ValueError, TypeError, KeyError):
            return None
    else:
        return None
    return {
        "kind": "verified_optional_not_disclosed",
        "state": state,
        "evidence": review,
        "meaning": "原文核验后未单列本期金额；仅移出可选填充分母，不是零或不适用",
    }


def review_disclosure_gaps(fields, sections, source, layout):
    """Check printed cells and search full consolidated amount-note sections.

    A verified blank remains numerically missing. No numeric zero is added.
    The search is reproducible and bounded to the source, not an assertion that
    the company has no such asset, liability or transaction.
    """
    from . import MONEY, compact, row_label

    meta = source["metadata"]
    notes = [s for s in sections if s["kind"] == "notes"]
    note_blocks = [s for s in sections if s["kind"] == "notes_block"]
    searched = [
        {"section_id": s["id"], "pages": s["pages"], "title": s["title"]}
        for s in note_blocks
    ]
    reviews = {}
    for metric, field in fields.items():
        if field["status"] in {"obtained", "not_applicable"}:
            continue
        _, statement, pattern = FIELDS[metric]
        proof, chosen = None, None
        candidates = field.get("candidates", [])
        unresolved_number = any(
            c.get("numbers")
            and c.get("cell_status", {}).get("current") != "blank_in_statement"
            for c in candidates
        )
        candidates = (
            []
            if unresolved_number or field["status"] == "conflict"
            else sorted(
                candidates,
                key=lambda c: (
                    c["source_ref"]["line_end"] - c["source_ref"]["line_start"]
                ),
                reverse=True,
            )
        )
        for candidate in candidates:
            comparative_only = (
                candidate.get("cell_status", {}).get("current") == "blank_in_statement"
            )
            if (candidate.get("numbers") and not comparative_only) or not candidate.get(
                "columns_verified"
            ):
                continue
            ref = candidate["source_ref"]
            if len(ref["pages"]) != 1:
                continue
            for section in sections:
                if section["kind"] != statement:
                    continue
                window = [
                    r
                    for r in section["lines"]
                    if r["page"] == ref["pages"][0]
                    and ref["line_start"] <= r["line"] <= ref["line_end"]
                    and r["text"].strip()
                ]
                if "\n".join(r["text"] for r in window) != candidate["quote"]:
                    continue
                proof = blank_current_cell(
                    window,
                    section,
                    layout,
                    int(meta["period_end"][:4]),
                    meta["period_end"],
                )
                if proof:
                    chosen = candidate
                    break
            if proof:
                break
        hits = []
        for note in notes:
            title = re.sub(r"^\s*\d+\s*[、．.]\s*", "", note["title"]).strip()
            exact_topic = re.fullmatch(pattern, compact(title)) is not None
            note_rows = [r for r in note["lines"] if r["text"].strip()]
            for i, row in enumerate(note_rows):
                if not (
                    exact_topic
                    or re.fullmatch(pattern, row_label(row["text"]).rstrip("（）() "))
                ):
                    continue
                window = note_rows[max(0, i - 1) : i + 3]
                quote = "\n".join(r["text"] for r in window)
                if MONEY.search(quote):
                    hits.append(
                        {
                            "section_id": note["id"],
                            "pages": sorted({r["page"] for r in window}),
                            "quote": quote,
                            "meaning": "possible note amount; scope and column require review",
                        }
                    )
                    if exact_topic:
                        break
        # No notes block is not evidence that the complete notes were searched.
        field.setdefault("raw_extraction_status", field["status"])
        field.setdefault("raw_extraction_reason", field.get("reason"))
        if field["status"] == "conflict":
            state = "conflict"
        elif proof and searched and not hits:
            state = "verified_blank"
            field.update(
                status="not_disclosed",
                reason="verified_blank_current_statement_cell; no matching amount in searched consolidated notes; not zero",
            )
        elif proof:
            state = "verified_blank_note_review_required"
            field.update(
                status="not_located",
                reason="verified_blank_current_statement_cell; note coverage or numeric candidates require review",
            )
        elif not field.get("candidates"):
            state = "not_located"
        else:
            state = "unresolved_extraction"
        review = {
            "version": VERSION,
            "state": state,
            "pdf_sha256": meta.get("sha256"),
            "period_end": meta["period_end"],
            "statement_evidence": {
                "source_ref": chosen["source_ref"],
                "quote": chosen["quote"],
                "coordinate_evidence": proof,
            }
            if chosen
            else None,
            "searched_note_blocks": searched,
            "note_numeric_candidates": hits,
            "search_pattern": pattern,
            "scope": "current consolidated statement cell and complete located consolidated amount-note blocks",
            "numeric_eligible": False,
            "limitation": "No numeric value inferred; this is not evidence of economic zero or blanket non-applicability.",
        }
        field["disclosure_review"] = review
        reviews[metric] = review
    health = {"version": VERSION, "details": reviews}
    apply_review_receipt(health, fields, source)
    verified = [
        m
        for m, r in reviews.items()
        if r["state"] in {"verified_blank", "manual_verified_disclosure_gap"}
    ]
    pending = [m for m in reviews if m not in verified]
    return {
        **health,
        "source_limited_fields": verified,
        "unresolved_fields": pending,
        "details": reviews,
        "raw_numeric_gate_unchanged": True,
        "status": "requires_review"
        if health.get("receipt_errors")
        else "source_limited"
        if verified and not pending
        else "requires_review"
        if pending
        else "complete",
    }


def merge_verified_image_health(document):
    """Close only fields independently verified by the image material checker."""
    from copy import deepcopy

    health = deepcopy(document.get("extraction_health", {}))
    document["text_extraction_health"] = deepcopy(health)
    if not health:
        return
    gate = document["gate"]
    verified = {
        m: refs
        for m, refs in gate.get("image_evidence_fields", {}).items()
        if refs
        and m in gate.get("usable_fields", [])
        and m not in gate.get("missing_fields", {})
    }
    for metric, refs in verified.items():
        previous = health.get("details", {}).get(metric)
        if previous is not None:
            previous["text_state"] = previous.get("state")
            previous.update(state="verified_original_image", image_evidence_refs=refs)
    health["unresolved_fields"] = [
        m for m in health.get("unresolved_fields", []) if m not in verified
    ]
    health["source_limited_fields"] = [
        m for m in health.get("source_limited_fields", []) if m not in verified
    ]
    health["image_verified_fields"] = sorted(verified)
    health["status"] = (
        "requires_review"
        if health["unresolved_fields"] or health.get("receipt_errors")
        else "source_limited"
        if health["source_limited_fields"]
        else "complete"
    )
    document["extraction_health"] = health
