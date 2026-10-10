"""Quarantine mechanically invalid legacy model proposals without altering sources."""

from .contracts import fingerprint, validate_packet


def quarantine_legacy_proposals(service, packet):
    issuer = packet["company"]["id"]
    responses = {
        fingerprint(fact): event["id"]
        for event in service.store.events(kind="model_response", company_id=issuer)
        if event["payload"].get("role") == "extract"
        for fact in event["payload"].get("content", {}).get("facts", [])
        if isinstance(fact, dict)
    }
    extractions = {
        fingerprint(fact): event["id"]
        for event in service.store.events(kind="extraction", company_id=issuer)
        for fact in event["payload"].get("facts", [])
        if isinstance(fact, dict)
    }
    rejected, retained = [], []
    for fact in packet.get("facts", []):
        key = fingerprint(fact)
        if key not in responses or key not in extractions:
            retained.append(fact)
            continue
        source = next(
            (s for s in packet["sources"] if s["id"] == fact.get("source_id")), None
        )
        if source is None:
            retained.append(fact)
            continue
        _, errors = validate_packet(
            {**packet, "facts": [fact], "sources": [source]}, service.today
        )
        invalid = [
            e
            for e in errors
            if e["code"]
            in {
                "unit_conversion_mismatch",
                "invalid_numeric_provenance",
                "value_not_in_quote",
            }
        ]
        if not invalid:
            retained.append(fact)
            continue
        rejected.append(
            {
                "fact_id": fact["id"],
                "proposal_hash": key,
                "source_id": fact["source_id"],
                "model_response_event": responses[key],
                "extraction_event": extractions[key],
                "errors": invalid,
            }
        )
    if not rejected:
        return
    # The original response/extraction audit and every source remain untouched.
    # Valid but contradictory observations are retained for conflict review.
    packet["facts"] = retained
    packet["extraction_quarantine"] = rejected
    service._event(
        issuer,
        "legacy_extraction_quarantine",
        {
            "version": "fq-invalid-proposal-quarantine/1",
            "proposals": rejected,
        },
    )
