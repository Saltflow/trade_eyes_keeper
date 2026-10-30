"""Source-backed company metadata and explicit peer candidates; no research calls."""

from __future__ import annotations

import json
from datetime import date
from itertools import combinations
from pathlib import Path

from .disclosure import verified_pack

FIELDS = (
    "industry",
    "business",
    "segments",
    "geographies",
    "business_model",
    "accounting_standard",
)


def build_metadata(manifest: dict, pack_root: Path) -> dict:
    """Claims require exact quoted source pages; co-configuration is no proof.

    Industry matches nominate peers for later assessment. They do not establish
    economic comparability, and differences never silently exclude a company.
    """
    date.fromisoformat(manifest["as_of"])
    companies = manifest["companies"]
    ids = [c["issuer_id"] for c in companies]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("company_identifiers_must_be_nonempty_and_unique")
    facts = {issuer: {field: [] for field in FIELDS} for issuer in ids}
    for group in manifest.get("candidate_groups", []):
        if any(i not in facts for i in group["issuer_ids"]):
            raise ValueError("peer_candidate_contains_unknown_company")
    for claim in manifest.get("claims", []):
        issuer, field = claim["issuer_id"], claim["field"]
        if (
            issuer not in facts
            or field not in FIELDS
            or not isinstance(claim.get("value"), str)
            or not claim["value"].strip()
        ):
            raise ValueError("invalid_company_metadata_claim")
        ref = claim["source"]
        key = ref["pack_key"]
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid_metadata_pack_key")
        pack, source = verified_pack(pack_root / "packs" / key)
        meta = pack["metadata"]
        if meta["issuer_id"] != issuer or meta["published_at"][:10] > manifest["as_of"]:
            raise ValueError("metadata_source_identity_or_date_mismatch")
        pages, quote = ref["pages"], ref["quote"]
        if not pages or not isinstance(quote, str) or not quote.strip():
            raise ValueError("metadata_source_quote_required")
        if claim["value"] not in quote:
            raise ValueError("metadata_value_must_be_literal_in_quote")
        if any(type(p) is not int or p <= 0 for p in pages):
            raise ValueError("metadata_pages_must_be_positive_integers")
        text = "\n".join(source["pages"][str(p)] for p in pages)
        if quote not in text:
            raise ValueError("metadata_quote_not_in_source_pages")
        facts[issuer][field].append(claim)
    rows = []
    for company in companies:
        metadata = {}
        for field, claims in facts[company["issuer_id"]].items():
            values = {
                json.dumps(c["value"], sort_keys=True, ensure_ascii=False)
                for c in claims
            }
            metadata[field] = {
                "status": "missing"
                if not values
                else "source_linked"
                if len(values) == 1
                else "conflict",
                "claims": claims,
            }
        rows.append(
            {
                **company,
                "metadata": metadata,
                "missing": [k for k, v in metadata.items() if v["status"] == "missing"],
            }
        )
    peers = []
    for left, right in combinations(rows, 2):
        left_id, right_id = left["issuer_id"], right["issuer_id"]
        labels = []
        for group in manifest.get("candidate_groups", []):
            if {left_id, right_id} <= set(group["issuer_ids"]):
                labels.append(group["label"])
        industries = [r["metadata"]["industry"] for r in (left, right)]
        same = all(v["status"] == "source_linked" for v in industries) and (
            industries[0]["claims"][0]["value"] == industries[1]["claims"][0]["value"]
        )
        peers.append(
            {
                "issuers": [left_id, right_id],
                "status": "candidate" if labels or same else "undetermined",
                "basis": {"configured_groups": labels, "same_disclosed_industry": same},
                "comparability": "pending",
                "consider": [
                    "业务结构",
                    "收入地区",
                    "商业模式",
                    "会计口径",
                    "合并范围",
                ],
                "missing": {r["issuer_id"]: r["missing"] for r in (left, right)},
            }
        )
    return {
        "schema_version": "fundamental-metadata/1",
        "as_of": manifest["as_of"],
        "configuration_source": manifest.get("configuration_source"),
        "companies": rows,
        "peer_pairs": peers,
        "model_calls": 0,
        "acceptance": "source_linkage_only_semantic_review_pending",
    }
