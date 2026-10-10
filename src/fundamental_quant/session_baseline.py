"""Reuse structured financial observations as background, never as page evidence."""

from datetime import date


def financial_baseline(packet, as_of):
    issuer = packet.get("company", {}).get("id")
    cutoff = date.fromisoformat(str(as_of))
    sources = {
        s["id"]: s
        for s in packet.get("sources", [])
        if s.get("kind") == "statement" and s.get("issuer_id") == issuer
    }
    eligible, excluded = [], {}
    for fact in packet.get("facts", []):
        if fact.get("source_id") not in sources:
            continue
        source = sources[fact["source_id"]]
        try:
            period = date.fromisoformat(fact["period_end"])
            published = date.fromisoformat(fact["published_at"])
        except (KeyError, TypeError, ValueError):
            reason = "missing_or_invalid_date"
        else:
            reason = None
            if period > cutoff or published > cutoff:
                reason = "future_observation"
            elif fact.get("company_id", issuer) != issuer:
                reason = "company_mismatch"
            elif any(
                source.get(key) and source[key] != fact.get(key)
                for key in ("period_end", "published_at")
            ):
                reason = "source_date_conflict"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            eligible.append(fact)
    years = sorted({f["period_end"][:4] for f in eligible}, reverse=True)[:7]
    observations = [f for f in eligible if f["period_end"][:4] in years]
    observations.sort(key=lambda f: (f["period_end"], f.get("metric", ""), f["id"]))
    refs = {f["source_id"] for f in observations}
    return {
        "status": "reference_only" if observations else "unavailable",
        "observations": observations,
        "sources": [
            {key: value for key, value in sources[ref].items() if key != "text"}
            for ref in sorted(refs)
        ],
        "excluded": excluded,
        "scope": "Latest seven available fiscal years; original values, units and verification flags retained.",
        "notice": "Reference observations, not independently verified PDF facts. Do not infer zero from missing values or fabricate page citations. Recheck decisive conflicts and accounting interpretation against original documents.",
    }
