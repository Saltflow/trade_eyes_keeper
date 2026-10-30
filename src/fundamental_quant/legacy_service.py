"""Pure helpers for preserved legacy consumers; no executable service is exposed."""

from __future__ import annotations

import copy

from .contracts import fingerprint


class ResearchBlocked(RuntimeError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(item["code"] for item in errors))


def evidence_hash(packet, *, exclude_prices=False):
    """Wall-clock/as-of refresh alone is not new evidence or a retry permit."""
    value = {
        key: packet.get(key)
        for key in (
            "company",
            "facts",
            "sources",
            "peers",
            "peer_candidates",
            "peer_packets",
            "page_reviews",
        )
    }
    value["page_reviews"] = value["page_reviews"] or []
    if exclude_prices:
        value = copy.deepcopy(value)
        for company in [value.get("company"), *(value.get("peer_candidates") or [])]:
            if isinstance(company, dict):
                for field in (
                    "market_cap",
                    "quote_as_of",
                    "size_status",
                    "membership_as_of",
                ):
                    company.pop(field, None)
        value["peer_packets"] = [
            evidence_hash(p, exclude_prices=True)
            for p in value.get("peer_packets") or []
        ]
        quote_ids = {
            s["id"] for s in value.get("sources") or [] if s.get("kind") == "quote"
        }
        value["sources"] = [
            s for s in value.get("sources") or [] if s["id"] not in quote_ids
        ]
        value["facts"] = [
            f for f in value.get("facts") or [] if f.get("source_id") not in quote_ids
        ]
    return fingerprint(value)
