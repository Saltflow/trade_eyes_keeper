"""Company history and reading files, independent of research orchestration.

Internal fact records remain unchanged. The reading format is Markdown with CSV;
source/lineage records are separate files, not repeated beside every number.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path


def csv_text(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue()


def assemble_history(
    pack_root: Path, *, max_chars: int, as_of: str, issuer_id: str | None = None
) -> dict:
    """Exact repeat removal only; no implicit period selection or truncation.

    The character budget is explicit, not a guessed model token count. All full
    blocks and occurrence coordinates survive even if the context is rejected.
    """
    from datetime import date

    from .disclosure import minimum_document, read_json, verified_pack

    date.fromisoformat(as_of)
    if max_chars <= 0:
        raise ValueError("max_chars_must_be_positive")
    run = read_json(pack_root / "latest_run.json")
    documents, blocked = [], []
    for entry in run["entries"]:
        if not entry.get("pack_key"):
            blocked.append(entry)
            continue
        pack, _ = verified_pack(pack_root / "packs" / entry["pack_key"])
        if issuer_id is not None and pack["metadata"]["issuer_id"] != issuer_id:
            continue
        if pack["metadata"]["published_at"][:10] > as_of:
            raise ValueError("history_contains_future_disclosure")
        documents.append(minimum_document(pack))
    if not documents:
        raise ValueError("no_disclosures_for_history")
    if len({d["metadata"]["issuer_id"] for d in documents}) != 1:
        raise ValueError("history_requires_one_company")
    documents.sort(
        key=lambda d: (
            d["metadata"]["issuer_id"],
            d["metadata"]["period_end"],
            d["metadata"]["published_at"],
            d["pack_key"],
        )
    )
    blocks, repeats, seen = [], {}, set()
    for doc in documents:
        if doc["pack_key"] in seen:
            raise ValueError("duplicate_disclosure_version")
        seen.add(doc["pack_key"])
        meta = doc["metadata"]
        origin = {
            k: meta.get(k)
            for k in (
                "issuer_id",
                "company_name",
                "period_end",
                "kind",
                "published_at",
                "sha256",
            )
        }
        origin["pack_key"] = doc["pack_key"]
        financial = csv_text(
            [
                ["metric", "status", "amount", "currency", "unit", "scope"],
                *[
                    [
                        key,
                        field["status"],
                        field.get("amount", ""),
                        field.get("currency", ""),
                        field.get("unit", ""),
                        field.get("scope", ""),
                    ]
                    for key, field in doc["financial_fields"].items()
                ],
            ]
        )
        # Proxy boundaries, missing topics and previous-policy references must
        # remain visible alongside the reading text, not be lost by compression.
        details = {
            k: doc[k]
            for k in (
                "fcf_proxy",
                "management_discussion",
                "accounting_topics",
                "supplements",
                "unexpanded_source_refs",
            )
        }
        details["financial_sources"] = {
            metric: {
                key: value
                for key, value in field.items()
                if key
                in {
                    "source_ref",
                    "quote",
                    "period_end",
                    "comparative_period_end",
                    "currency_status",
                    "verification",
                }
            }
            for metric, field in doc["financial_fields"].items()
        }
        blocks.append(
            {
                "kind": "financial",
                "text": financial
                + json.dumps(details, ensure_ascii=False, sort_keys=True),
                "occurrences": [origin],
            }
        )
        for section in doc["sections"]:
            occurrence = {
                **origin,
                "section_id": section["id"],
                "pages": section["pages"],
            }
            cfo = doc["financial_fields"]["cfo"]
            key = (
                meta["issuer_id"],
                meta.get("accounting_standard"),
                meta.get("scope", cfo.get("scope")),
                cfo.get("currency"),
                section["kind"],
                section["title"],
                section["text"],
            )
            reusable = section["kind"] in {"policy", "notes", "other_notes"}
            if reusable and key in repeats:
                blocks[repeats[key]]["occurrences"].append(occurrence)
            else:
                if reusable:
                    repeats[key] = len(blocks)
                blocks.append(
                    {
                        "kind": section["kind"],
                        "title": section["title"],
                        "text": section["text"],
                        "occurrences": [occurrence],
                    }
                )
    rendered = "\n\n".join(
        f"## B{i}: {block.get('title', block['kind'])}\n"
        + json.dumps(block["occurrences"], ensure_ascii=False, sort_keys=True)
        + "\n"
        + block["text"]
        for i, block in enumerate(blocks, 1)
    )
    within = len(rendered) <= max_chars
    coverage = []
    for issuer in sorted({d["metadata"]["issuer_id"] for d in documents}):
        metas = [
            d["metadata"] for d in documents if d["metadata"]["issuer_id"] == issuer
        ]
        annual_years = {
            int(m["period_end"][:4]) for m in metas if m["kind"] == "annual"
        }
        missing = (
            sorted(set(range(min(annual_years), max(annual_years) + 1)) - annual_years)
            if annual_years
            else []
        )
        coverage.append(
            {
                "issuer_id": issuer,
                "periods": [
                    {
                        "period_end": m["period_end"],
                        "kind": m["kind"],
                        "published_at": m["published_at"],
                    }
                    for m in metas
                ],
                "annual_years_not_in_selected_inputs": missing,
            }
        )
    return {
        "schema_version": "fundamental-history/1",
        "issuer_id": documents[0]["metadata"]["issuer_id"],
        "as_of": as_of,
        "status": "ready" if within and not blocked else "blocked",
        "max_chars": max_chars,
        "context_chars": len(rendered),
        "overflow_chars": max(0, len(rendered) - max_chars),
        "context": rendered if within and not blocked else None,
        "documents": [d["pack_key"] for d in documents],
        "coverage": coverage,
        "blocks": blocks,
        "blocked_documents": blocked,
        "repeat_occurrences_removed": sum(len(b["occurrences"]) - 1 for b in blocks),
        "model_calls": 0,
    }
