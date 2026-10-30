"""Narrow proposal repairs; callers must run the ordinary extraction validator."""

from __future__ import annotations

import copy
import hashlib

from .contracts import fingerprint

VERSION = "local-currency-scope-repair/1"


def repair_local_currency_scope(packet: dict, proposals: list) -> tuple[list, list]:
    """Correct only the scope tag on an identical local unit/currency header.

    This does not certify any financial fact. The caller must preserve the raw
    response and validate repaired proposals, including original model issues,
    with prepare_extracted_facts before accepting them. No numeric value, source,
    quotation, metric, period, unit, or currency is changed.
    """
    repaired = copy.deepcopy(proposals)
    sources = {}
    ambiguous = set()
    for source in packet.get("sources", []):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            continue
        source_id = source["id"]
        if source_id in sources:
            ambiguous.add(source_id)
        sources[source_id] = source
    audit = []
    for index, proposal in enumerate(repaired):
        if not isinstance(proposal, dict):
            continue
        source_id = proposal.get("source_id")
        if not isinstance(source_id, str) or source_id in ambiguous:
            continue
        if not (
            proposal.get("currency_scope") == "financial_statements"
            and source_id == proposal.get("unit_source_id")
            and source_id == proposal.get("currency_source_id")
        ):
            continue
        quote = proposal.get("currency_quote")
        source = sources.get(source_id, {})
        text = source.get("text")
        if not (
            isinstance(quote, str)
            and quote.strip()
            and quote == proposal.get("unit_quote")
            and isinstance(text, str)
            and quote in text
        ):
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if source.get("hash") != digest:
            continue
        original = copy.deepcopy(proposal)
        proposal["currency_scope"] = None
        audit.append(
            {
                "repair_version": VERSION,
                "proposal_index": index,
                "proposal_id": original.get("id"),
                "source_id": source_id,
                "source_hash": digest,
                "reason": "identical_verbatim_local_unit_currency_header",
                "changed_field": "currency_scope",
                "before": "financial_statements",
                "after": None,
                "original_proposal": original,
                "original_proposal_hash": fingerprint(original),
                "repaired_proposal_hash": fingerprint(proposal),
                "requires_extraction_validation": True,
            }
        )
    return repaired, audit
